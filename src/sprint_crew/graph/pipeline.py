from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any

from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from sprint_crew.agents.coder import normalize_change, run_coder_with_coverage
from sprint_crew.agents.formatter import run_formatter
from sprint_crew.agents.reviewer import run_reviewer
from sprint_crew.agents.tech_lead_planning import run_tech_lead_validated
from sprint_crew.agents.tester import run_tester_loop, run_tester_reporter
from sprint_crew.agents.tool_events import tool_call_events
from sprint_crew.config import Role, get_settings
from sprint_crew.graph.lanes import ensure_lane, stop_lane
from sprint_crew.graph.pipeline_helpers import (
    _acceptance_failure_dict,
    _coverage_from_dict,
    _coverage_satisfied,
    _deadline_epoch,
    _deadline_exceeded,
    _in_backlog_batch,
    _timed_detail,
)
from sprint_crew.graph.pipeline_phases import _phased
from sprint_crew.graph.state import (
    SprintState,
    code_change_from_state,
    task_plan_from_state,
    ticket_from_state,
    workspace_from_state,
)
from sprint_crew.inference.router import coder_thinking_active
from sprint_crew.orchestrator.acceptance_failure import (
    analyze_acceptance_output,
)
from sprint_crew.orchestrator.acceptance_tests import run_acceptance_tests
from sprint_crew.orchestrator.merge_gate import review_accepted
from sprint_crew.orchestrator.plan_coverage import (
    coverage_improved,
    should_invoke_tester,
)
from sprint_crew.orchestrator.plan_validation import snapshot_baseline_paths
from sprint_crew.orchestrator.repo_context import (
    enrich_repo_context_with_hits,
    maybe_index_workspace,
    pre_search_agent_event,
)
from sprint_crew.orchestrator.retry import (
    format_review_feedback,
    resolve_failure_feedback,
    resolve_retry_scope,
)
from sprint_crew.orchestrator.ship_cycle import orchestrator_ship
from sprint_crew.orchestrator.template_plan import work_lane_required_for_ticket
from sprint_crew.orchestrator.workspace_diff import gather_workspace_diff
from sprint_crew.schemas.change import ReviewOutcome
from sprint_crew.schemas.session import AgentEvent, SessionStatus
from sprint_crew.schemas.session import agent_event as _event
from sprint_crew.schemas.ticket import TaskPlan


async def _stop_lane_after_cycle(state: SprintState, role: Role) -> None:
    if _in_backlog_batch(state):
        return
    await stop_lane(role)


async def _swap_lane(stop: Role, start: Role) -> None:
    """Only one lane may be loaded at a time on 128 GB unified memory (AGENTS.md 4.1),
    so every lane change is a stop followed by a start, never a start alone."""
    await stop_lane(stop)
    await ensure_lane(start)


def _diff_for(state: SprintState, workspace: Path, plan: TaskPlan) -> str:
    """Reuse the diff already gathered this cycle, or compute it. Recomputing costs a git
    subprocess per node, which is why the state value is preferred when present."""
    return state.get("workspace_diff") or gather_workspace_diff(
        workspace, priority_paths=plan.files_to_touch
    )


async def init_session(state: SprintState) -> dict[str, Any]:
    started = time.monotonic()
    workspace = workspace_from_state(state)
    git_dir = workspace / ".git"
    if not git_dir.exists():
        raise ValueError(f"Workspace is not a git repo: {workspace}")
    ticket = ticket_from_state(state)
    template_fast_path = not work_lane_required_for_ticket(ticket)

    session_id = state["session_id"]
    index_result = await asyncio.to_thread(
        maybe_index_workspace,
        workspace,
        session_id,
        ticket=ticket,
    )
    events: list[AgentEvent] = []
    if index_result is not None:
        if index_result.chunks >= 0:
            events.append(
                _event(
                    "orchestrator",
                    "vector_indexed",
                    f"Indexed {index_result.chunks} chunks from {index_result.files} files",
                    chunks=index_result.chunks,
                    files=index_result.files,
                    seconds=index_result.seconds,
                    git_sha=index_result.git_sha,
                ),
            )
        else:
            events.append(
                _event(
                    "orchestrator",
                    "vector_index_skipped",
                    "Vector index unchanged — git SHA matches existing collection",
                    git_sha=index_result.git_sha,
                ),
            )

    events.append(
        _event(
            "orchestrator",
            "session_started",
            f"Session {state['session_id']} started",
            **_timed_detail(started),
        ),
    )

    return {
        "attempt": state.get("attempt", 0),
        "status": SessionStatus.RUNNING,
        "prior_review_feedback": state.get("prior_review_feedback", ""),
        "plan_retries": state.get("plan_retries", 0),
        "skip_tester_this_attempt": False,
        "tests_run_this_cycle": False,
        "acceptance_test_output": "",
        "template_fast_path": template_fast_path,
        "events": events,
    }


async def tech_lead_plan(state: SprintState) -> dict[str, Any]:
    started = time.monotonic()
    ticket = ticket_from_state(state)
    workspace = workspace_from_state(state)
    session_id = state["session_id"]
    baseline = frozenset(state.get("baseline_paths") or snapshot_baseline_paths(workspace))

    work_lane_required = not state.get("template_fast_path", False)

    if work_lane_required:
        await ensure_lane(Role.WORK)

    events: list[AgentEvent] = []
    plan_query = f"{ticket.summary}\n{ticket.description}"
    context_text, pre_hits = enrich_repo_context_with_hits(
        workspace,
        session_id,
        plan_query,
        ticket=ticket,
    )
    if pre_hits:
        events.append(pre_search_agent_event(plan_query, pre_hits))
    try:
        plan, planning_mode, tech_lead_tool_log = await run_tech_lead_validated(
            ticket,
            workspace,
            session_id=session_id,
            prior_review_feedback=state.get("prior_review_feedback", ""),
            baseline_paths=baseline,
            pre_search_hit_count=len(pre_hits),
            repo_context=context_text,
        )
    except RuntimeError as exc:
        events.append(
            _event(
                "orchestrator",
                "plan_aborted",
                f"TechLead planning failed for {ticket.key}",
                level="error",
                error=str(exc),
            ),
        )
        return {
            "status": SessionStatus.FAILED,
            "error": str(exc),
            "baseline_paths": sorted(baseline),
            "events": events,
        }
    finally:
        if work_lane_required:
            await stop_lane(Role.WORK)

    detail: dict[str, Any] = {
        "mode": planning_mode,
        "steps": len(plan.steps),
        **_timed_detail(started),
    }
    if work_lane_required:
        detail["lane"] = "work"

    events.extend(tool_call_events("tech_lead", tech_lead_tool_log))

    events.append(
        _event(
            "tech_lead",
            "plan_created",
            f"TaskPlan for {plan.ticket_key}: {plan.summary}",
            **detail,
        ),
    )

    return {
        "task_plan": plan.model_dump(),
        "baseline_paths": sorted(baseline),
        "events": events,
    }


async def code_implement(state: SprintState) -> dict[str, Any]:
    started = time.monotonic()
    plan = task_plan_from_state(state)
    workspace = workspace_from_state(state)
    baseline = frozenset(state.get("baseline_paths") or ())
    attempt = state.get("attempt", 0)

    await ensure_lane(Role.CODING)
    (
        raw_output,
        tool_log,
        coverage,
        acceptance_output,
        acceptance_verified,
    ) = await run_coder_with_coverage(
        plan,
        workspace,
        prior_review_feedback=state.get("prior_review_feedback", ""),
        baseline_paths=baseline or None,
        deadline_epoch=_deadline_epoch(state),
        attempt=attempt,
    )
    workspace_diff = gather_workspace_diff(workspace, priority_paths=plan.files_to_touch)

    await _swap_lane(Role.CODING, Role.WORK)
    change = await run_formatter(
        task_plan=plan,
        raw_output=raw_output,
        git_diff=workspace_diff,
    )
    change = normalize_change(change, plan)

    events: list[AgentEvent] = [
        *tool_call_events("coder", tool_log),
        _event(
            "coder",
            "code_change",
            f"CodeChange for {change.ticket_key}: tests_passed={change.tests_passed}",
            **_timed_detail(
                started,
                lane="coding+work",
                attempt=attempt,
                thinking=coder_thinking_active(attempt),
            ),
        ),
    ]
    if not coverage.satisfied:
        events.append(
            _event(
                "orchestrator",
                "plan_coverage_incomplete",
                "Plan coverage incomplete after continuation rounds",
                level="warning",
                missing=coverage.missing,
                unexpected=coverage.unexpected,
                out_of_scope_hits=coverage.out_of_scope_hits,
                phantom_paths=coverage.phantom_paths,
            ),
        )

    tests_run = acceptance_verified or change.tests_passed
    result: dict[str, Any] = {
        "code_change": change.model_dump(),
        "workspace_diff": workspace_diff,
        "branch": change.branch,
        "plan_coverage": {
            "satisfied": coverage.satisfied,
            "missing": coverage.missing,
            "unexpected": coverage.unexpected,
            "out_of_scope_hits": coverage.out_of_scope_hits,
            "blocking_unexpected": coverage.blocking_unexpected,
            "phantom_paths": coverage.phantom_paths,
        },
        "tests_run_this_cycle": tests_run,
        "events": events,
    }
    if acceptance_output:
        result["acceptance_test_output"] = acceptance_output
    return result


async def test_implement(state: SprintState) -> dict[str, Any]:
    started = time.monotonic()
    plan = task_plan_from_state(state)
    change = code_change_from_state(state)
    workspace = workspace_from_state(state)

    if state.get("skip_tester_this_attempt"):
        workspace_diff = _diff_for(state, workspace, plan)
        return {
            "workspace_diff": workspace_diff,
            "skip_tester_this_attempt": False,
            "acceptance_failure": {},
            "events": [
                _event(
                    "tester",
                    "skipped",
                    "Code retry with green tests — tester not needed",
                    **_timed_detail(started),
                ),
            ],
        }

    if state.get("tests_run_this_cycle") and state.get("acceptance_test_output"):
        acceptance_output = str(state["acceptance_test_output"])
        acceptance_green = True
    else:
        acceptance_output, acceptance_green = await asyncio.to_thread(
            run_acceptance_tests,
            workspace,
            plan.acceptance_tests,
        )
    failure_analysis = (
        analyze_acceptance_output(acceptance_output) if acceptance_output.strip() else None
    )

    if not should_invoke_tester(
        plan,
        workspace,
        acceptance_green=acceptance_green,
        acceptance_output=acceptance_output,
    ):
        workspace_diff = _diff_for(state, workspace, plan)
        if acceptance_green:
            skip_reason = "Acceptance tests green — tester skipped"
        elif failure_analysis is not None and not failure_analysis.tester_can_help:
            skip_reason = "Source/build failure — tester cannot edit src/; coder must fix first"
        else:
            skip_reason = "No test gap — tester skipped"
        return {
            "workspace_diff": workspace_diff,
            "tests_run_this_cycle": acceptance_green,
            "acceptance_test_output": acceptance_output,
            "acceptance_failure": _acceptance_failure_dict(failure_analysis),
            "events": [
                _event(
                    "tester",
                    "skipped",
                    skip_reason,
                    **_timed_detail(started),
                ),
            ],
        }

    await _swap_lane(Role.WORK, Role.CODING)
    raw_output, tool_log = await run_tester_loop(
        plan,
        change,
        workspace,
        acceptance_green=acceptance_green,
        acceptance_output=acceptance_output,
    )
    workspace_diff = gather_workspace_diff(workspace, priority_paths=plan.files_to_touch)

    await _swap_lane(Role.CODING, Role.WORK)
    additions = await run_tester_reporter(plan, raw_output)

    if additions is None:
        return {
            "workspace_diff": workspace_diff,
            "tests_run_this_cycle": True,
            "acceptance_failure": _acceptance_failure_dict(failure_analysis),
            "events": [
                *tool_call_events("tester", tool_log),
                _event(
                    "tester",
                    "skipped",
                    "No additional tests required by plan",
                    **_timed_detail(started, lane="coding+work"),
                ),
            ],
        }
    return {
        "test_additions": additions.model_dump(),
        "workspace_diff": workspace_diff,
        "tests_run_this_cycle": additions.tests_passed,
        "acceptance_failure": _acceptance_failure_dict(failure_analysis),
        "events": [
            *tool_call_events("tester", tool_log),
            _event(
                "tester",
                "tests_added",
                f"TestAdditions: {len(additions.tests_added)} files, passed={additions.tests_passed}",
                **_timed_detail(started, lane="coding+work"),
            ),
        ],
    }


async def review(state: SprintState) -> dict[str, Any]:
    started = time.monotonic()
    await _swap_lane(Role.CODING, Role.WORK)
    plan = task_plan_from_state(state)
    change = code_change_from_state(state)
    workspace = workspace_from_state(state)
    workspace_diff = _diff_for(state, workspace, plan)
    test_additions_json = ""
    if raw_additions := state.get("test_additions"):
        test_additions_json = json.dumps(raw_additions, indent=2)

    coverage = state.get("plan_coverage", {})
    coverage_summary = ""
    if isinstance(coverage, dict) and not coverage.get("satisfied", True):
        coverage_summary = (
            f"missing={coverage.get('missing', [])}; unexpected={coverage.get('unexpected', [])}"
        )

    tests_already_run = bool(state.get("tests_run_this_cycle", False) and change.tests_passed)
    outcome = await run_reviewer(
        plan,
        change,
        workspace,
        workspace_diff=workspace_diff,
        test_additions_json=test_additions_json,
        ticket_acceptance_criteria=ticket_from_state(state).acceptance_criteria,
        tests_already_run=tests_already_run,
        coverage_summary=coverage_summary,
        files_to_touch=plan.files_to_touch,
    )
    await _stop_lane_after_cycle(state, Role.WORK)
    return {
        "review_outcome": outcome.model_dump(),
        "workspace_diff": workspace_diff,
        "events": [
            _event(
                "reviewer",
                "review_complete",
                f"ReviewOutcome passed={outcome.passed} tests_passed={outcome.tests_passed}",
                findings=len(outcome.findings),
                **_timed_detail(started, lane="work"),
            ),
        ],
    }


async def merge_gate(state: SprintState) -> dict[str, Any]:
    started = time.monotonic()
    outcome = ReviewOutcome.model_validate(state["review_outcome"])
    coverage_ok = _coverage_satisfied(state)
    accepted = review_accepted(outcome, coverage_satisfied=coverage_ok)
    block_reason: str | None = None
    if not accepted:
        if not coverage_ok:
            block_reason = "coverage"
        elif not outcome.tests_passed:
            block_reason = "tests"
        elif not outcome.passed:
            block_reason = "review"
        else:
            block_reason = "unknown"
    return {
        "events": [
            _event(
                "merge_gate",
                "gate_result",
                "accepted" if accepted else "rejected",
                accepted=accepted,
                attempt=state.get("attempt", 0),
                coverage_satisfied=coverage_ok,
                block_reason=block_reason,
                **_timed_detail(started),
            ),
        ],
    }


def route_after_plan(state: SprintState) -> str:
    if state.get("status") == SessionStatus.FAILED:
        return "failed"
    if _deadline_exceeded(state):
        return "failed"
    return "code"


def route_after_gate(state: SprintState) -> str:
    outcome = ReviewOutcome.model_validate(state["review_outcome"])
    if review_accepted(outcome, coverage_satisfied=_coverage_satisfied(state)):
        return "ship"
    attempt = state.get("attempt", 0)
    if attempt >= get_settings().max_review_retries:
        return "failed"
    if state.get("coverage_stall_count", 0) >= 2:
        return "failed"
    if _deadline_exceeded(state):
        return "failed"
    return "retry"


async def prepare_retry(state: SprintState) -> dict[str, Any]:
    started = time.monotonic()

    outcome = ReviewOutcome.model_validate(state["review_outcome"])
    plan = task_plan_from_state(state)
    workspace_diff = state.get("workspace_diff", "")
    change = code_change_from_state(state) if state.get("code_change") else None
    coverage_raw = state.get("plan_coverage")
    coverage = _coverage_from_dict(coverage_raw)
    scope = resolve_retry_scope(
        outcome,
        coverage=coverage,
        workspace_root=workspace_from_state(state),
    )

    plan_retries = state.get("plan_retries", 0)
    if scope == "plan":
        plan_retries += 1

    stall_count = state.get("coverage_stall_count", 0)
    prev_raw = state.get("plan_coverage_prev")
    if coverage is not None:
        prev = _coverage_from_dict(prev_raw)
        if prev is not None and not coverage_improved(prev, coverage):
            stall_count += 1
        else:
            stall_count = 0

    skip_tester = (
        scope == "code"
        and state.get("tests_run_this_cycle", False)
        and change is not None
        and change.tests_passed
    )

    test_output = ""
    if not skip_tester:
        cached = state.get("acceptance_test_output", "")
        if state.get("tests_run_this_cycle") and cached:
            test_output = str(cached)
        else:
            test_output, _ = await asyncio.to_thread(
                run_acceptance_tests,
                workspace_from_state(state),
                plan.acceptance_tests,
            )

    failure_analysis, bugs_observed = resolve_failure_feedback(
        test_output=test_output,
        acceptance_failure=state.get("acceptance_failure"),
        test_additions=state.get("test_additions")
        if isinstance(state.get("test_additions"), dict)
        else None,
    )

    feedback = format_review_feedback(
        outcome,
        workspace_diff=workspace_diff,
        test_output=test_output,
        coverage=coverage,
        workspace_root=workspace_from_state(state),
        failure_analysis=failure_analysis,
        bugs_observed=bugs_observed,
    )
    return {
        "attempt": state.get("attempt", 0) + 1,
        "prior_review_feedback": feedback,
        "plan_retries": plan_retries,
        "skip_tester_this_attempt": skip_tester,
        "retry_scope": scope,
        "coverage_stall_count": stall_count,
        "plan_coverage_prev": coverage_raw if isinstance(coverage_raw, dict) else {},
        "events": [
            _event(
                "orchestrator",
                "retry_prepared",
                f"Retry attempt {state.get('attempt', 0) + 1} scope={scope}",
                level="warning",
                attempt=state.get("attempt", 0) + 1,
                feedback_preview=feedback[:500],
                retry_scope=scope,
                skip_tester=skip_tester,
                coverage_stall_count=stall_count,
                **_timed_detail(started),
            ),
        ],
    }


def route_after_retry(state: SprintState) -> str:
    if _deadline_exceeded(state):
        return "failed"
    scope = state.get("retry_scope")
    if scope not in {"plan", "code"}:
        outcome = ReviewOutcome.model_validate(state["review_outcome"])
        coverage_raw = state.get("plan_coverage")
        coverage = _coverage_from_dict(coverage_raw)
        scope = resolve_retry_scope(
            outcome,
            coverage=coverage,
            workspace_root=workspace_from_state(state),
        )
    if scope == "plan" and state.get("plan_retries", 0) > get_settings().max_plan_retries:
        return "code"
    return scope


async def awaiting_human(state: SprintState) -> dict[str, Any]:
    return {
        "status": SessionStatus.AWAITING_HUMAN,
        "events": [_event("orchestrator", "awaiting_human", "Ready for human PR review/merge")],
    }


async def failed(state: SprintState) -> dict[str, Any]:
    outcome = ReviewOutcome.model_validate(state.get("review_outcome", {}))
    coverage_raw = state.get("plan_coverage")
    coverage = _coverage_from_dict(coverage_raw)
    if _deadline_exceeded(state):
        summary = "Per-cycle wall-clock budget exceeded"
    elif state.get("coverage_stall_count", 0) >= 2:
        summary = "Coverage stalled across retries"
    else:
        summary = "Max review retries exceeded"
    return {
        "status": SessionStatus.FAILED,
        "error": format_review_feedback(
            outcome,
            workspace_diff=state.get("workspace_diff", ""),
            coverage=coverage,
        ),
        "events": [
            _event(
                "orchestrator",
                "failed",
                summary,
                level="error",
                coverage_stall_count=state.get("coverage_stall_count", 0),
                deadline_exceeded=_deadline_exceeded(state),
            )
        ],
    }


def build_sprint_graph(*, checkpointer: Any | None = None) -> CompiledStateGraph:
    graph: StateGraph = StateGraph(SprintState)
    # Phase-bracketed nodes: the ones that do real work and can run for minutes.
    # ``awaitingHuman`` and ``failed`` are terminal bookkeeping that already emit their own
    # event, so a phase pair around them would only add noise.
    for name, node in (
        ("initSession", init_session),
        ("techLeadPlan", tech_lead_plan),
        ("codeImplement", code_implement),
        ("testImplement", test_implement),
        ("review", review),
        ("mergeGate", merge_gate),
        ("prepareRetry", prepare_retry),
        ("orchestratorShip", orchestrator_ship),
    ):
        graph.add_node(name, _phased(name, node))
    graph.add_node("awaitingHuman", awaiting_human)
    graph.add_node("failed", failed)

    graph.add_edge(START, "initSession")
    graph.add_edge("initSession", "techLeadPlan")
    graph.add_conditional_edges(
        "techLeadPlan",
        route_after_plan,
        {
            "code": "codeImplement",
            "failed": "failed",
        },
    )
    graph.add_edge("codeImplement", "testImplement")
    graph.add_edge("testImplement", "review")
    graph.add_edge("review", "mergeGate")
    graph.add_conditional_edges(
        "mergeGate",
        route_after_gate,
        {
            "ship": "orchestratorShip",
            "retry": "prepareRetry",
            "failed": "failed",
        },
    )
    graph.add_conditional_edges(
        "prepareRetry",
        route_after_retry,
        {
            "plan": "techLeadPlan",
            "code": "codeImplement",
            "failed": "failed",
        },
    )
    graph.add_edge("orchestratorShip", "awaitingHuman")
    graph.add_edge("awaitingHuman", END)
    graph.add_edge("failed", END)

    return graph.compile(checkpointer=checkpointer)


async def run_sprint_cycle(
    state: SprintState,
    *,
    on_node_complete: Any | None = None,
) -> SprintState:
    settings = get_settings()
    settings.checkpoint_db.parent.mkdir(parents=True, exist_ok=True)
    async with AsyncSqliteSaver.from_conn_string(str(settings.checkpoint_db)) as checkpointer:
        app = build_sprint_graph(checkpointer=checkpointer)
        config = {"configurable": {"thread_id": state["session_id"]}}
        accumulated: dict[str, Any] = dict(state)
        async for chunk in app.astream(state, config, stream_mode="updates"):
            for _node_name, update in chunk.items():
                if not isinstance(update, dict):
                    continue
                for key, value in update.items():
                    if key == "events" and key in accumulated and isinstance(value, list):
                        accumulated["events"] = list(accumulated.get("events", [])) + value
                    else:
                        accumulated[key] = value
                if on_node_complete is not None:
                    await on_node_complete(dict(accumulated))
        return accumulated  # type: ignore[return-value]
