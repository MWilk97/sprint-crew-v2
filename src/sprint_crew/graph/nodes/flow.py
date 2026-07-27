"""Conditional-edge functions and the two terminal nodes.

Grouped because none of them do agent work: they read state, pick an edge, and record how
the cycle ended. They are also the only readers of the retry budgets in Settings, which the
graph tests patch in one place.
"""

from __future__ import annotations

from typing import Any

from sprint_crew.config import get_settings
from sprint_crew.graph.nodes._support import (
    _coverage_from_dict,
    _coverage_satisfied,
    _deadline_exceeded,
)
from sprint_crew.graph.state import SprintState, workspace_from_state
from sprint_crew.orchestrator.merge_gate import review_accepted
from sprint_crew.orchestrator.retry import format_review_feedback, resolve_retry_scope
from sprint_crew.schemas.change import ReviewOutcome
from sprint_crew.schemas.session import SessionStatus
from sprint_crew.schemas.session import agent_event as _event

# --- conditional edges -------------------------------------------------------


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
        scope = resolve_retry_scope(
            outcome,
            coverage=_coverage_from_dict(state.get("plan_coverage")),
            workspace_root=workspace_from_state(state),
        )
    if scope == "plan" and state.get("plan_retries", 0) > get_settings().max_plan_retries:
        return "code"
    return scope


# --- terminal nodes ----------------------------------------------------------


async def awaiting_human(state: SprintState) -> dict[str, Any]:
    return {
        "status": SessionStatus.AWAITING_HUMAN,
        "events": [_event("orchestrator", "awaiting_human", "Ready for human PR review/merge")],
    }


async def failed(state: SprintState) -> dict[str, Any]:
    """Terminal failure. Reports *why*, which means not overwriting an upstream reason.

    ``techLeadPlan`` routes here with ``status``/``error`` already set when planning aborts;
    rebuilding ``error`` from an empty ReviewOutcome threw the real message away.
    """
    upstream_error = str(state.get("error") or "")
    if upstream_error:
        return {
            "status": SessionStatus.FAILED,
            "error": upstream_error,
            "events": [
                _event(
                    "orchestrator",
                    "failed",
                    "Planning aborted",
                    level="error",
                    error=upstream_error,
                    attempt=state.get("attempt", 0),
                )
            ],
        }

    if _deadline_exceeded(state):
        summary = "Per-cycle wall-clock budget exceeded"
    elif state.get("coverage_stall_count", 0) >= 2:
        summary = "Coverage stalled across retries"
    else:
        summary = "Max review retries exceeded"
    return {
        "status": SessionStatus.FAILED,
        "error": format_review_feedback(
            ReviewOutcome.model_validate(state.get("review_outcome", {})),
            workspace_diff=state.get("workspace_diff", ""),
            coverage=_coverage_from_dict(state.get("plan_coverage")),
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
