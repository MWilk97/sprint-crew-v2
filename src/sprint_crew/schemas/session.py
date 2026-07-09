from __future__ import annotations

from datetime import UTC, datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from sprint_crew.schemas.change import CodeChange, ReviewOutcome, TestAdditions
from sprint_crew.schemas.ticket import JiraTicket, TaskPlan

_STRICT = ConfigDict(extra="forbid")


def _utc_now_iso() -> str:
    return datetime.now(tz=UTC).isoformat()


class SessionStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    AWAITING_HUMAN = "awaiting_human"
    FAILED = "failed"
    APPROVED = "approved"


class AgentEvent(BaseModel):
    model_config = _STRICT

    timestamp: str = Field(default_factory=_utc_now_iso)
    agent: str = Field(..., min_length=1)
    event_type: str = Field(..., min_length=1)
    summary: str = Field(..., min_length=1)
    detail: dict[str, Any] | None = None


class SprintSession(BaseModel):
    model_config = _STRICT

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
    events: list[AgentEvent] = Field(default_factory=list)
    error: str | None = None
    attempt: int = Field(default=0, ge=0)
    created_at: str = Field(default_factory=_utc_now_iso)
    updated_at: str = Field(default_factory=_utc_now_iso)


class BacklogRunStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


class BacklogRun(BaseModel):
    model_config = _STRICT

    run_id: str = Field(..., min_length=1)
    status: BacklogRunStatus
    user_prompt: str = Field(..., min_length=1)
    session_ids: list[str] = Field(default_factory=list)
    completed_session_ids: list[str] = Field(default_factory=list)
    failed_ticket_key: str | None = None
    repo_url: str | None = None
    error: str | None = None
    created_at: str = Field(default_factory=_utc_now_iso)
