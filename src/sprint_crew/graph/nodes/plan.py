"""Session bootstrap and TechLead planning."""

from __future__ import annotations

import asyncio
import time
from typing import Any

from sprint_crew.agents import tech_lead_planning
from sprint_crew.agents.tool_events import tool_call_events
from sprint_crew.config import Role
from sprint_crew.graph import lanes
from sprint_crew.graph.pipeline_helpers import (
    _timed_detail,
)
from sprint_crew.graph.state import (
    SprintState,
    ticket_from_state,
    workspace_from_state,
)
from sprint_crew.orchestrator.plan_validation import snapshot_baseline_paths
from sprint_crew.orchestrator.repo_context import (
    enrich_repo_context_with_hits,
    maybe_index_workspace,
    pre_search_agent_event,
)
from sprint_crew.orchestrator.template_plan import work_lane_required_for_ticket
from sprint_crew.schemas.session import AgentEvent, SessionStatus
from sprint_crew.schemas.session import agent_event as _event


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
        await lanes.ensure_lane(Role.WORK)

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
        plan, planning_mode, tech_lead_tool_log = await tech_lead_planning.run_tech_lead_validated(
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
            await lanes.stop_lane(Role.WORK)

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
