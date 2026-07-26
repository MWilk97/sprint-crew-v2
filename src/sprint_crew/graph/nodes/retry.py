"""Retry preparation: scope, feedback, and the acceptance-failure analysis."""

from __future__ import annotations

import time
from typing import Any

from sprint_crew.graph.pipeline_helpers import (
    _coverage_from_dict,
    _timed_detail,
)
from sprint_crew.graph.state import (
    SprintState,
    code_change_from_state,
    task_plan_from_state,
    workspace_from_state,
)
from sprint_crew.orchestrator import acceptance_tests, plan_coverage
from sprint_crew.orchestrator.retry import (
    format_review_feedback,
    resolve_failure_feedback,
    resolve_retry_scope,
)
from sprint_crew.schemas.change import ReviewOutcome
from sprint_crew.schemas.session import agent_event as _event


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
        if prev is not None and not plan_coverage.coverage_improved(prev, coverage):
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
            test_output, _ = await acceptance_tests.run_acceptance_tests(
                workspace_from_state(state), plan.acceptance_tests
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
