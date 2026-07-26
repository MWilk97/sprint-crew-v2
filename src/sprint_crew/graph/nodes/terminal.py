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
