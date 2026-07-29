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
        return "review"
    # Rounds the user sent back are discounted: ``attempt`` counts every pass because it
    # keys the diff snapshot, but a picky human must not spend the machine's retries (M7).
    attempt = state.get("attempt", 0) - state.get("user_rejection_rounds", 0)
    if attempt >= get_settings().max_review_retries:
        return "failed"
    if state.get("coverage_stall_count", 0) >= 2:
        return "failed"
    if _deadline_exceeded(state):
        return "failed"
    return "retry"


def route_after_diff_review(state: SprintState) -> str:
    """Ship what the user accepted, or send their rejections round again (M7).

    The gate node has already decided the terminal cases (timeout, exhausted budget) and
    said so in ``failure_reason``; this stays a pure read of state, like its neighbours.

    The wall-clock budget is deliberately *not* consulted here. By this point the merge
    gate has accepted and a human has signed off, so the work is done — a budget exists to
    stop new work starting, and failing here only throws away a finished story.
    """
    if state.get("failure_reason"):
        return "failed"
    decisions = state.get("review_decisions") or []
    rejected = any(d.get("decision") == "reject" for d in decisions)
    return "retry" if rejected else "ship"


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


# Every way a cycle can end badly, as a slug a client can branch on. Two of these are not
# crashes at all (the M7 human gate), and telling them apart by parsing the error prose
# would be guesswork — so all five carry the slug, not just the new ones.
_FAILURE_SUMMARIES = {
    "plan_aborted": "Planning aborted",
    "review_timeout": "Diff review expired without a decision",
    "review_budget_exhausted": "User rejection budget exhausted",
    "deadline_exceeded": "Per-cycle wall-clock budget exceeded",
    "coverage_stalled": "Coverage stalled across retries",
    "review_retries_exhausted": "Max review retries exceeded",
}


async def failed(state: SprintState) -> dict[str, Any]:
    """Terminal failure. Reports *why*, which means not overwriting an upstream reason.

    ``techLeadPlan`` routes here with ``status``/``error`` already set when planning aborts;
    rebuilding ``error`` from an empty ReviewOutcome threw the real message away.
    """
    upstream_error = str(state.get("error") or "")
    if upstream_error:
        reason = str(state.get("failure_reason") or "plan_aborted")
        return {
            "status": SessionStatus.FAILED,
            "error": upstream_error,
            "events": [
                _event(
                    "orchestrator",
                    "failed",
                    _FAILURE_SUMMARIES.get(reason, "Planning aborted"),
                    level="error",
                    error=upstream_error,
                    attempt=state.get("attempt", 0),
                    reason=reason,
                )
            ],
        }

    if _deadline_exceeded(state):
        reason = "deadline_exceeded"
    elif state.get("coverage_stall_count", 0) >= 2:
        reason = "coverage_stalled"
    else:
        reason = "review_retries_exhausted"
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
                _FAILURE_SUMMARIES[reason],
                level="error",
                reason=reason,
                coverage_stall_count=state.get("coverage_stall_count", 0),
                deadline_exceeded=_deadline_exceeded(state),
            )
        ],
    }
