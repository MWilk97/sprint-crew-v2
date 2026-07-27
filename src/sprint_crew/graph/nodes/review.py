"""Review and the deterministic merge gate."""

from __future__ import annotations

import json
import time
from typing import Any

from sprint_crew.agents import reviewer
from sprint_crew.config import Role
from sprint_crew.graph.nodes._support import (
    _coverage_satisfied,
    _diff_for,
    _stop_lane_after_cycle,
    _swap_lane,
    _timed_detail,
)
from sprint_crew.graph.state import (
    SprintState,
    code_change_from_state,
    task_plan_from_state,
    ticket_from_state,
    workspace_from_state,
)
from sprint_crew.orchestrator.merge_gate import review_accepted
from sprint_crew.schemas.change import ReviewOutcome
from sprint_crew.schemas.session import agent_event as _event


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
    outcome = await reviewer.run_reviewer(
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
