"""Retry preparation: scope, feedback, and the acceptance-failure analysis.

Two entry points, deliberately not one. ``prepare_retry`` answers a *machine* rejection and
has to re-derive why (acceptance output, coverage stall, reviewer findings).
``prepare_rejection_retry`` answers a *human* one: the reason is already written, the gates
already passed, and re-running acceptance tests to rediscover that would cost minutes for
nothing.
"""

from __future__ import annotations

import time
from typing import Any

from sprint_crew.graph.nodes._support import (
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
    escalate_scope_for_build_failure,
    format_review_feedback,
    format_user_rejection_feedback,
    resolve_failure_feedback,
    resolve_rejection_scope,
    resolve_retry_scope,
)
from sprint_crew.schemas.change import ReviewOutcome
from sprint_crew.schemas.diff import FileDecision
from sprint_crew.schemas.session import agent_event as _event


def _skip_tester(state: SprintState, scope: str) -> bool:
    """Whether the Tester can sit out the next attempt.

    Its tests were written last round and passed; a code-only retry rewrites source, not
    tests, so re-running the agent buys nothing. Acceptance tests still re-run either way.
    """
    if scope != "code" or not state.get("tests_run_this_cycle", False):
        return False
    change = code_change_from_state(state) if state.get("code_change") else None
    return change is not None and change.tests_passed


async def prepare_retry(state: SprintState) -> dict[str, Any]:
    started = time.monotonic()

    outcome = ReviewOutcome.model_validate(state["review_outcome"])
    plan = task_plan_from_state(state)
    workspace_diff = state.get("workspace_diff", "")
    coverage_raw = state.get("plan_coverage")
    coverage = _coverage_from_dict(coverage_raw)
    scope = resolve_retry_scope(
        outcome,
        coverage=coverage,
        workspace_root=workspace_from_state(state),
    )

    stall_count = state.get("coverage_stall_count", 0)
    prev_raw = state.get("plan_coverage_prev")
    if coverage is not None:
        prev = _coverage_from_dict(prev_raw)
        if prev is not None and not plan_coverage.coverage_improved(prev, coverage):
            stall_count += 1
        else:
            stall_count = 0

    # Provisional: only decides whether the acceptance output has to be re-read below. The
    # binding value is recomputed once the analysis may have moved scope to "plan".
    test_output = ""
    if not _skip_tester(state, scope):
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

    # Only now is it known *why* the tests failed, and a source/build failure changes who
    # can fix it — so scope is settled here, after the analysis, not before it.
    scope = escalate_scope_for_build_failure(scope, failure_analysis)
    plan_retries = state.get("plan_retries", 0) + (1 if scope == "plan" else 0)
    skip_tester = _skip_tester(state, scope)

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


async def prepare_rejection_retry(state: SprintState) -> dict[str, Any]:
    """Turn a human's per-file rejections into the next attempt (roadmap M7).

    ``attempt`` still advances — it keys the diff snapshot, so freezing it would make this
    round overwrite the very capture the user reviewed. The budget is kept honest at the
    gate instead: ``route_after_gate`` discounts rejection rounds from MAX_REVIEW_RETRIES.
    """
    started = time.monotonic()

    decisions = [FileDecision.model_validate(d) for d in state.get("review_decisions", [])]
    rejected = [d for d in decisions if d.decision == "reject"]
    accepted = [d.path for d in decisions if d.decision == "accept"]
    scope = resolve_rejection_scope(rejected)
    feedback = format_user_rejection_feedback(rejected, accepted_paths=accepted)

    skip_tester = _skip_tester(state, scope)
    plan_retries = state.get("plan_retries", 0) + (1 if scope == "plan" else 0)
    rounds = state.get("user_rejection_rounds", 0) + 1

    return {
        "attempt": state.get("attempt", 0) + 1,
        "user_rejection_rounds": rounds,
        "prior_review_feedback": feedback,
        "plan_retries": plan_retries,
        "skip_tester_this_attempt": skip_tester,
        "retry_scope": scope,
        # Cleared so a later pass cannot route on this round's verdicts.
        "review_decisions": [],
        "events": [
            _event(
                "orchestrator",
                "rejection_recorded",
                f"{len(rejected)} file(s) rejected by the user (round {rounds}) scope={scope}",
                level="warning",
                attempt=state.get("attempt", 0) + 1,
                rejection_round=rounds,
                retry_scope=scope,
                rejected=[d.path for d in rejected],
                accepted=accepted,
                feedback_preview=feedback[:500],
                skip_tester=skip_tester,
                **_timed_detail(started),
            ),
        ],
    }
