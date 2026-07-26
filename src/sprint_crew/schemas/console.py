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

from sprint_crew.schemas.session import AgentEvent

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
    # Started, waiting for the single run slot (roadmap M5). One run at a time is a GPU
    # constraint, not a policy — see orchestrator/run_registry.py.
    QUEUED = "queued"
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
    # What this choice costs or buys — shown next to the option so the user can judge it.
    rationale: str | None = None


class ClarifyQuestion(BaseModel):
    model_config = _STRICT

    question_id: str = Field(..., min_length=1)
    text: str = Field(..., min_length=1)
    suggestions: list[ClarifySuggestion] = Field(..., min_length=1)
    allow_custom: bool = True
    # Which ambiguity prompted the question; null for legacy/fallback questions.
    why_asked: str | None = None
    # The answer the backend would pick if the user said "just decide".
    recommended_suggestion_id: str | None = None

    @model_validator(mode="after")
    def _recommendation_is_offered(self) -> ClarifyQuestion:
        if self.recommended_suggestion_id is not None and self.recommended_suggestion_id not in {
            s.suggestion_id for s in self.suggestions
        }:
            raise ValueError("recommended_suggestion_id must match one of the suggestions")
        return self


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


class IntentSummary(BaseModel):
    """What the Interpreter understood, echoed back so the user can correct it."""

    model_config = _STRICT

    restated_goal: str = Field(..., min_length=1)
    assumptions: list[str] = Field(default_factory=list)
    unknowns: list[str] = Field(default_factory=list)
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)


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
    intent: IntentSummary | None = None
    clarify_questions: list[ClarifyQuestion] = Field(default_factory=list)
    clarify_answers: list[ClarifyAnswer] = Field(default_factory=list)
    sprint_ref: SprintRunRef | None = None
    plan_result: ConsolePlanResult | None = None
    error: str | None = None
    # How many runs must finish before this one starts (M5). Non-null only while ``queued``,
    # where it is 1 or more; null the moment the run is admitted or the session ends.
    queue_position: int | None = Field(default=None, ge=1)
    # Set when Stop was accepted but the run has not stopped yet. A field rather than a
    # ``cancelling`` status: the pending state is additive this way, so it does not break
    # a client's state machine, and it works for polling as well as for the event stream.
    cancel_requested_at: str | None = None
    created_at: str = Field(default_factory=_utc_now_iso)
    updated_at: str = Field(default_factory=_utc_now_iso)


class ConsoleEventsPage(BaseModel):
    """A page of the console-session timeline (roadmap M2).

    Served by polling ``GET /v1/console/sessions/{id}/events?since=&limit=``. ``seq``
    is monotonic across every sprint session the console run spawned; poll again with
    ``since=next_seq`` for the next page. ``complete`` is true once the console session
    reaches a terminal status, so a client knows no more events will arrive.
    """

    model_config = _STRICT

    events: list[AgentEvent] = Field(default_factory=list)
    next_seq: int = 0
    complete: bool = False
