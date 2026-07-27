"""Tester node: decides whether to run, then runs and reports."""

from __future__ import annotations

import time
from typing import Any

from sprint_crew.agents import tester
from sprint_crew.agents.tool_events import tool_call_events
from sprint_crew.config import Role
from sprint_crew.graph.nodes._support import (
    _acceptance_failure_dict,
    _diff_for,
    _swap_lane,
    _timed_detail,
)
from sprint_crew.graph.state import (
    SprintState,
    code_change_from_state,
    task_plan_from_state,
    workspace_from_state,
)
from sprint_crew.orchestrator import acceptance_tests, plan_coverage
from sprint_crew.orchestrator import workspace_diff as diff_tools
from sprint_crew.orchestrator.acceptance_failure import analyze_acceptance_output
from sprint_crew.schemas.session import agent_event as _event


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
        acceptance_output, acceptance_green = await acceptance_tests.run_acceptance_tests(
            workspace, plan.acceptance_tests
        )
    failure_analysis = (
        analyze_acceptance_output(acceptance_output) if acceptance_output.strip() else None
    )

    if not plan_coverage.should_invoke_tester(
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
    raw_output, tool_log = await tester.run_tester_loop(
        plan,
        change,
        workspace,
        acceptance_green=acceptance_green,
        acceptance_output=acceptance_output,
    )
    workspace_diff = diff_tools.gather_workspace_diff(workspace, priority_paths=plan.files_to_touch)

    await _swap_lane(Role.CODING, Role.WORK)
    additions = await tester.run_tester_reporter(plan, raw_output)

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
