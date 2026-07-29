"""Coder implementation node."""

from __future__ import annotations

import asyncio
import time
from typing import Any

from sprint_crew.agents import coder_coverage, formatter
from sprint_crew.agents.coder import normalize_change
from sprint_crew.agents.tool_events import tool_call_events
from sprint_crew.config import Role
from sprint_crew.graph import lanes
from sprint_crew.graph.nodes._support import (
    _deadline_epoch,
    _swap_lane,
    _timed_detail,
)
from sprint_crew.graph.state import (
    SprintState,
    index_scope_from_state,
    task_plan_from_state,
    workspace_from_state,
)
from sprint_crew.inference.router import coder_thinking_active
from sprint_crew.orchestrator import workspace_diff as diff_tools
from sprint_crew.orchestrator.acceptance_tests import tool_log_shows_passing_acceptance
from sprint_crew.orchestrator.repo_context import maybe_index_overlay
from sprint_crew.schemas.session import AgentEvent
from sprint_crew.schemas.session import agent_event as _event


async def code_implement(state: SprintState) -> dict[str, Any]:
    started = time.monotonic()
    plan = task_plan_from_state(state)
    workspace = workspace_from_state(state)
    baseline = frozenset(state.get("baseline_paths") or ())
    attempt = state.get("attempt", 0)

    await lanes.ensure_lane(Role.CODING)
    (
        raw_output,
        tool_log,
        coverage,
        acceptance_output,
        acceptance_verified,
    ) = await coder_coverage.run_coder_with_coverage(
        plan,
        workspace,
        prior_review_feedback=state.get("prior_review_feedback", ""),
        baseline_paths=baseline or None,
        deadline_epoch=_deadline_epoch(state),
        attempt=attempt,
    )
    workspace_diff = diff_tools.gather_workspace_diff(workspace, priority_paths=plan.files_to_touch)

    # Refresh the run's overlay with what the Coder just wrote (roadmap M8). Until now the
    # index was built once at session start and went stale the moment a file was edited,
    # so every later semantic_search answered from pre-edit content.
    await asyncio.to_thread(maybe_index_overlay, workspace, index_scope_from_state(state))

    await _swap_lane(Role.CODING, Role.WORK)
    change = await formatter.run_formatter(
        task_plan=plan,
        raw_output=raw_output,
        git_diff=workspace_diff,
    )
    change = normalize_change(change, plan)

    # A green claim the evidence does not support is corrected here rather than carried:
    # acceptance_verified is the orchestrator's own run, and the tool log is the agent's.
    # If neither shows a passing acceptance command, tests_passed is not a judgement call
    # the Coder gets to make.
    unverified_claim = (
        change.tests_passed
        and not acceptance_verified
        and bool(plan.acceptance_tests)
        and not tool_log_shows_passing_acceptance(tool_log, plan.acceptance_tests, workspace)
    )
    if unverified_claim:
        change = change.model_copy(update={"tests_passed": False})

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
    if unverified_claim:
        events.append(
            _event(
                "orchestrator",
                "unverified_tests_claim",
                "Coder reported tests_passed with no passing acceptance run on record",
                level="warning",
                acceptance_tests=plan.acceptance_tests,
            ),
        )
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

    # Only the orchestrator's own run counts. "Not verified" is not a synonym for "green",
    # and this flag is what lets the Reviewer skip its re-run.
    tests_run = acceptance_verified
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
