"""Conditional-edge functions.

All three live together because they are the only readers of retry budgets from
Settings, and the graph tests patch that in one place.
"""

from __future__ import annotations

from sprint_crew.config import get_settings
from sprint_crew.graph.pipeline_helpers import (
    _coverage_from_dict,
    _coverage_satisfied,
    _deadline_exceeded,
)
from sprint_crew.graph.state import (
    SprintState,
    workspace_from_state,
)
from sprint_crew.orchestrator.merge_gate import review_accepted
from sprint_crew.orchestrator.retry import (
    resolve_retry_scope,
)
from sprint_crew.schemas.change import ReviewOutcome
from sprint_crew.schemas.session import SessionStatus


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
