"""Schemas for the /v1/console/* API (roadmap Phase 1).

Contracts are documented in docs/contracts/chat-console-api.md and
docs/contracts/chat-console.openapi.yaml (ADR 0011 / ADR 0012). Served by the
live routes in sprint_crew.api.console (MVP in-memory store + deterministic
clarify stub).
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field, model_validator

_STRICT = ConfigDict(extra="forbid")


def _utc_now_iso() -> str:
    return datetime.now(tz=UTC).isoformat()


class ConsoleMode(str, Enum):
    PLAN = "plan"
    CODE = "code"


class ConsoleSessionStatus(str, Enum):
    COLLECTING = "collecting"
    CLARIFYING = "clarifying"
    READY = "ready"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class ConsoleMessageRole(str, Enum):
    USER = "user"
    ASSISTANT = "assistant"


class ConsoleMessage(BaseModel):
    model_config = _STRICT

    role: ConsoleMessageRole
    content: str = Field(..., min_length=1)
    timestamp: str = Field(default_factory=_utc_now_iso)


class ClarifySuggestion(BaseModel):
    model_config = _STRICT

    suggestion_id: str = Field(..., min_length=1)
    label: str = Field(..., min_length=1)
    detail: str | None = None


class ClarifyQuestion(BaseModel):
    model_config = _STRICT

    question_id: str = Field(..., min_length=1)
    text: str = Field(..., min_length=1)
    suggestions: list[ClarifySuggestion] = Field(..., min_length=1)
    allow_custom: bool = True


class ClarifyAnswer(BaseModel):
    model_config = _STRICT

    question_id: str = Field(..., min_length=1)
    selected_suggestion_id: str | None = None
    custom_text: str | None = None

    @model_validator(mode="after")
    def _exactly_one_answer(self) -> ClarifyAnswer:
        if (self.selected_suggestion_id is None) == (self.custom_text is None):
            raise ValueError("provide exactly one of selected_suggestion_id or custom_text")
        return self


class SprintRunRef(BaseModel):
    """Maps a started Code-mode console session to today's /sprint/* resources."""

    model_config = _STRICT

    backlog_run_id: str | None = None
    sprint_session_ids: list[str] = Field(default_factory=list)


class PlanPreviewStory(BaseModel):
    model_config = _STRICT

    title: str = Field(..., min_length=1)
    rationale: str | None = None


class ConsolePlanResult(BaseModel):
    """Plan-mode outcome: analysis only, never shipped (ADR 0012)."""

    model_config = _STRICT

    summary: str = Field(..., min_length=1)
    stories: list[PlanPreviewStory] = Field(default_factory=list)


class CreateConsoleSessionRequest(BaseModel):
    model_config = _STRICT

    mode: ConsoleMode
    initial_prompt: str | None = None
    repo_url: str | None = None
    # Phase 3 language-specialized lanes: nullable placeholder only.
    target_language: str | None = None


class PostMessageRequest(BaseModel):
    model_config = _STRICT

    content: str = Field(..., min_length=1)


class ClarifyRequest(BaseModel):
    model_config = _STRICT

    answers: list[ClarifyAnswer] = Field(..., min_length=1)


class ConsoleSession(BaseModel):
    model_config = _STRICT

    session_id: str = Field(..., min_length=1)
    mode: ConsoleMode
    status: ConsoleSessionStatus
    confirmed: bool = False
    repo_url: str | None = None
    target_language: str | None = None
    messages: list[ConsoleMessage] = Field(default_factory=list)
    clarify_questions: list[ClarifyQuestion] = Field(default_factory=list)
    clarify_answers: list[ClarifyAnswer] = Field(default_factory=list)
    sprint_ref: SprintRunRef | None = None
    plan_result: ConsolePlanResult | None = None
    error: str | None = None
    created_at: str = Field(default_factory=_utc_now_iso)
    updated_at: str = Field(default_factory=_utc_now_iso)


class ConsoleError(BaseModel):
    """Error body; matches FastAPI's HTTPException ``{"detail": ...}`` envelope."""

    model_config = _STRICT

    detail: str = Field(..., min_length=1)
