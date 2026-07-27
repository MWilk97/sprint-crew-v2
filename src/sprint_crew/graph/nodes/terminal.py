"""Terminal nodes: awaiting_human and failed."""

from __future__ import annotations

from typing import Any

from sprint_crew.graph.pipeline_helpers import (
    _coverage_from_dict,
    _deadline_exceeded,
)
from sprint_crew.graph.state import (
    SprintState,
)
from sprint_crew.orchestrator.retry import (
    format_review_feedback,
)
from sprint_crew.schemas.change import ReviewOutcome
from sprint_crew.schemas.session import SessionStatus
from sprint_crew.schemas.session import agent_event as _event


async def awaiting_human(state: SprintState) -> dict[str, Any]:
    return {
        "status": SessionStatus.AWAITING_HUMAN,
        "events": [_event("orchestrator", "awaiting_human", "Ready for human PR review/merge")],
    }


async def failed(state: SprintState) -> dict[str, Any]:
    """Terminal failure. Reports *why*, which means not overwriting an upstream reason.

    ``techLeadPlan`` routes here with ``status``/``error`` already set when planning
    aborts. Rebuilding ``error`` from an empty ReviewOutcome in that case threw the real
    message away and announced "Max review retries exceeded" on attempt 0 — misleading
    exactly when the field is being read to find out what happened.
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
