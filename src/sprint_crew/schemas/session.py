from __future__ import annotations

from datetime import UTC, datetime
from enum import Enum, StrEnum
from typing import Any, Literal

from pydantic import BaseModel, Field

from sprint_crew.schemas._base import STRICT
from sprint_crew.schemas.change import CodeChange, ReviewOutcome, TestAdditions
from sprint_crew.schemas.ticket import JiraTicket, TaskPlan


def utc_now_iso() -> str:
    return datetime.now(tz=UTC).isoformat()


class SessionStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    AWAITING_HUMAN = "awaiting_human"
    FAILED = "failed"
    APPROVED = "approved"
    # Domain can represent cancel; wired by RunRegistry / hard-cancel (roadmap M5).
    CANCELLED = "cancelled"


class EventType(StrEnum):
    """Closed vocabulary for ``AgentEvent.event_type`` (roadmap M2).

    The wire type stays ``str`` so an unknown future value from a newer backend
    degrades to "render generically" on the client instead of a 422. This enum is
    the source of truth for emitters and is grep-enforced by a unit test: every
    ``event_type`` string emitted in ``src/`` must be a member here.
    """

    SESSION_STARTED = "session_started"
    VECTOR_INDEXED = "vector_indexed"
    VECTOR_INDEX_SKIPPED = "vector_index_skipped"
    PRE_SEARCH = "pre_search"
    PLAN_CREATED = "plan_created"
    PLAN_ABORTED = "plan_aborted"
    TOOL_CALL = "tool_call"
    CODE_CHANGE = "code_change"
    PLAN_COVERAGE_INCOMPLETE = "plan_coverage_incomplete"
    SKIPPED = "skipped"
    TESTS_ADDED = "tests_added"
    REVIEW_COMPLETE = "review_complete"
    GATE_RESULT = "gate_result"
    RETRY_PREPARED = "retry_prepared"
    AWAITING_HUMAN = "awaiting_human"
    FAILED = "failed"
    SHIPPED = "shipped"
    SHIPPED_STUB = "shipped_stub"
    APPROVED = "approved"
    # M4 fine-grained progress
    LANE_LOADING = "lane_loading"
    LANE_READY = "lane_ready"
    LANE_STOPPED = "lane_stopped"
    PHASE_STARTED = "phase_started"
    PHASE_COMPLETED = "phase_completed"
    # M5 run lifecycle
    RUN_QUEUED = "run_queued"
    RUN_STARTED = "run_started"
    WORKSPACE_READY = "workspace_ready"
    BACKLOG_PLANNED = "backlog_planned"
    CANCEL_REQUESTED = "cancel_requested"
    CANCELLED = "cancelled"
    # M6 structured diffs
    DIFF_UPDATED = "diff_updated"
    # M7 human review gate
    AWAITING_DIFF_REVIEW = "awaiting_diff_review"
    REVIEW_DECISIONS_RECORDED = "review_decisions_recorded"
    REJECTION_RECORDED = "rejection_recorded"
    DIFF_REVIEW_EXPIRED = "diff_review_expired"
    REVIEW_BUDGET_EXHAUSTED = "review_budget_exhausted"


EventLevel = Literal["debug", "info", "warning", "error"]


class AgentEvent(BaseModel):
    model_config = STRICT

    timestamp: str = Field(default_factory=utc_now_iso)
    agent: str = Field(..., min_length=1)
    event_type: str = Field(..., min_length=1)
    summary: str = Field(..., min_length=1)
    detail: dict[str, Any] | None = None
    # M2 cursor/timeline fields. ``seq`` is stamped by the EventLog at append time
    # (an in-graph event cannot know its console-scoped sequence at construction),
    # so it is None on events living only inside SprintState / SprintSession.
    seq: int | None = None
    phase: str | None = None
    level: EventLevel = "info"


def agent_event(
    agent: str,
    event_type: str,
    summary: str,
    *,
    level: EventLevel = "info",
    **detail: Any,
) -> AgentEvent:
    """``level`` is keyword-only so it stays out of ``detail``, which absorbs everything else."""
    return AgentEvent(
        agent=agent,
        event_type=event_type,
        summary=summary,
        detail=detail or None,
        level=level,
    )


class SprintSession(BaseModel):
    model_config = STRICT

    session_id: str = Field(..., min_length=1)
    status: SessionStatus
    ticket_key: str = Field(..., min_length=1)
    workspace_root: str = Field(..., min_length=1)
    selected_ticket: JiraTicket | None = None
    task_plan: TaskPlan | None = None
    code_change: CodeChange | None = None
    review_outcome: ReviewOutcome | None = None
    test_additions: TestAdditions | None = None
    branch: str | None = None
    pr_url: str | None = None
    jira_url: str | None = None
    user_prompt: str | None = None
    backlog_run_id: str | None = None
    # Set when this sprint run was started from a console session (roadmap M2);
    # the events table joins on it so the console can serve one merged timeline.
    console_session_id: str | None = None
    events: list[AgentEvent] = Field(default_factory=list)
    error: str | None = None
    attempt: int = Field(default=0, ge=0)
    created_at: str = Field(default_factory=utc_now_iso)
    updated_at: str = Field(default_factory=utc_now_iso)


class BacklogRunStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    # Domain can represent cancel; wired by RunRegistry / hard-cancel (roadmap M5).
    CANCELLED = "cancelled"


class BacklogRun(BaseModel):
    model_config = STRICT

    run_id: str = Field(..., min_length=1)
    status: BacklogRunStatus
    user_prompt: str = Field(..., min_length=1)
    session_ids: list[str] = Field(default_factory=list)
    completed_session_ids: list[str] = Field(default_factory=list)
    failed_ticket_key: str | None = None
    repo_url: str | None = None
    error: str | None = None
    created_at: str = Field(default_factory=utc_now_iso)
