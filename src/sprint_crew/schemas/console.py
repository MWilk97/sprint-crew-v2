"""Schemas for the /v1/console/* API (roadmap Phase 1).

Contracts are documented in docs/contracts/chat-console-api.md and
docs/contracts/chat-console.openapi.yaml (ADR 0011 / ADR 0012). Served by the
live routes in sprint_crew.api.console (MVP in-memory store + deterministic
clarify stub).
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import Enum
from uuid import uuid4

from pydantic import BaseModel, Field, model_validator

from sprint_crew.schemas._base import STRICT
from sprint_crew.schemas.session import AgentEvent


def _utc_now_iso() -> str:
    return datetime.now(tz=UTC).isoformat()


def _message_id() -> str:
    return f"m-{uuid4().hex[:8]}"


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
    # The run is alive but parked on a per-file human verdict (roadmap M7). The first
    # non-terminal state that blocks on the *user* rather than on the backend: a client
    # must treat it as "your move", not as progress.
    AWAITING_REVIEW = "awaiting_review"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class WorkspaceStatus(str, Enum):
    """Progress of the session's own checkout (roadmap M8).

    Independent of the session status: a session is usable while its clone is still
    arriving, and the two reach their end states in either order.
    """

    PENDING = "pending"
    CLONING = "cloning"
    READY = "ready"
    FAILED = "failed"
    # Reclaimed by the workspace LRU. The session is still readable; its checkout is gone.
    EVICTED = "evicted"


class IndexStatus(str, Enum):
    PENDING = "pending"
    INDEXING = "indexing"
    READY = "ready"
    # Nothing to index, or indexing is switched off — not an error.
    SKIPPED = "skipped"
    FAILED = "failed"


class ConsoleMessageRole(str, Enum):
    USER = "user"
    ASSISTANT = "assistant"


class AnswerCitation(BaseModel):
    """Where an answer looked (roadmap M9).

    Derived from the Explainer's tool log rather than asked of the model: the paths it
    actually opened are a fact, whereas a model asked to cite itself invents line numbers.
    """

    model_config = STRICT

    path: str = Field(..., min_length=1)
    start_line: int | None = Field(default=None, ge=1)
    end_line: int | None = Field(default=None, ge=1)
    # Which tool surfaced it: "read_file" is a file the answer opened, "semantic_search" a
    # hit it was shown. A UI may rank the former higher.
    source: str = Field(..., min_length=1)


class ConsoleMessage(BaseModel):
    model_config = STRICT

    role: ConsoleMessageRole
    content: str = Field(..., min_length=1)
    # Stable identity for one turn (roadmap M9). An answer streams as a sequence of
    # answer_delta events carrying this id, so a client can grow the right bubble; without
    # it a session with two answers in flight cannot tell whose text just arrived.
    message_id: str = Field(default_factory=_message_id, min_length=1)
    # Only ever on an assistant turn produced by an ask.
    citations: list[AnswerCitation] = Field(default_factory=list)
    timestamp: str = Field(default_factory=_utc_now_iso)


class ClarifySuggestion(BaseModel):
    model_config = STRICT

    suggestion_id: str = Field(..., min_length=1)
    label: str = Field(..., min_length=1)
    detail: str | None = None
    # What this choice costs or buys — shown next to the option so the user can judge it.
    rationale: str | None = None


class ClarifyQuestion(BaseModel):
    model_config = STRICT

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
    model_config = STRICT

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

    model_config = STRICT

    restated_goal: str = Field(..., min_length=1)
    assumptions: list[str] = Field(default_factory=list)
    unknowns: list[str] = Field(default_factory=list)
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)


class SprintRunRef(BaseModel):
    """Maps a started Code-mode console session to today's /sprint/* resources."""

    model_config = STRICT

    backlog_run_id: str | None = None
    sprint_session_ids: list[str] = Field(default_factory=list)


class PlanPreviewStory(BaseModel):
    model_config = STRICT

    title: str = Field(..., min_length=1)
    rationale: str | None = None


class ConsolePlanResult(BaseModel):
    """Plan-mode outcome: analysis only, never shipped (ADR 0012)."""

    model_config = STRICT

    summary: str = Field(..., min_length=1)
    stories: list[PlanPreviewStory] = Field(default_factory=list)


class CreateConsoleSessionRequest(BaseModel):
    model_config = STRICT

    mode: ConsoleMode
    initial_prompt: str | None = None
    repo_url: str | None = None
    # Phase 3 language-specialized lanes: nullable placeholder only.
    target_language: str | None = None


class PostMessageRequest(BaseModel):
    model_config = STRICT

    content: str = Field(..., min_length=1)


class ClarifyRequest(BaseModel):
    model_config = STRICT

    answers: list[ClarifyAnswer] = Field(..., min_length=1)


class AskRequest(BaseModel):
    model_config = STRICT

    question: str = Field(..., min_length=1)


class ConsoleFile(BaseModel):
    """One file from the session's checkout, so a citation can be opened (roadmap M9)."""

    model_config = STRICT

    path: str = Field(..., min_length=1)
    content: str
    size_bytes: int = Field(..., ge=0)
    # True when the file exceeded CONSOLE_FILE_MAX_BYTES and ``content`` is a prefix.
    truncated: bool = False


class ConsoleSession(BaseModel):
    model_config = STRICT

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
    # Which clarify round the open questions belong to (roadmap M9). A message sent while
    # clarifying or ready re-runs the Interpreter, which *replaces* the question set; ids are
    # round-scoped (``q-{round}-{n}``) so an answer submitted against a superseded round is
    # rejected rather than silently matching a new question that happens to share an index.
    clarify_round: int = Field(default=1, ge=1)
    # Resolved Q&A from superseded rounds, kept as Interpreter context. Not answers: they
    # answer questions nobody is asking any more.
    prior_clarifications: list[str] = Field(default_factory=list)
    # The in-flight ask, if any (roadmap M9). Derived state — see ``ask_in_flight``.
    active_ask_id: str | None = None
    # Whether ``active_ask_id`` is still live in this process's RunRegistry. Recomputed on
    # read rather than owned, like ``awaiting_review``: a persisted boolean would survive a
    # restart that killed the task and leave a client's composer disabled forever.
    ask_in_flight: bool = False
    sprint_ref: SprintRunRef | None = None
    plan_result: ConsolePlanResult | None = None
    error: str | None = None
    # The session's own checkout and index, prepared in the background from creation
    # (roadmap M8). A second async dimension: these advance independently of ``status``,
    # so a client renders them separately rather than folding both into one spinner.
    workspace_status: WorkspaceStatus = WorkspaceStatus.PENDING
    workspace_root: str | None = None
    workspace_error: str | None = None
    index_status: IndexStatus = IndexStatus.PENDING
    index_error: str | None = None
    # How many runs must finish before this one starts (M5). Non-null only while ``queued``,
    # where it is 1 or more; null the moment the run is admitted or the session ends.
    queue_position: int | None = Field(default=None, ge=1)
    # Set when Stop was accepted but the run has not stopped yet. A field rather than a
    # ``cancelling`` status: the pending state is additive this way, so it does not break
    # a client's state machine, and it works for polling as well as for the event stream.
    cancel_requested_at: str | None = None
    created_at: str = Field(default_factory=_utc_now_iso)
    updated_at: str = Field(default_factory=_utc_now_iso)


class ConsoleSessionSummary(BaseModel):
    """One row of session history (roadmap M8).

    Deliberately not a whole ``ConsoleSession``: messages, clarify state and plan results
    are unbounded, and a history sidebar renders none of them.
    """

    model_config = STRICT

    session_id: str = Field(..., min_length=1)
    mode: ConsoleMode
    status: ConsoleSessionStatus
    workspace_status: WorkspaceStatus
    # First thing the user asked, truncated — what a sidebar shows as the session's name.
    title: str | None = None
    repo_url: str | None = None
    created_at: str
    updated_at: str


class ConsoleSessionPage(BaseModel):
    model_config = STRICT

    sessions: list[ConsoleSessionSummary] = Field(default_factory=list)
    total: int = 0
    # Null when this page is the last one.
    next_offset: int | None = None


class ConsoleEventsPage(BaseModel):
    """A page of the console-session timeline (roadmap M2).

    Served by polling ``GET /v1/console/sessions/{id}/events?since=&limit=``. ``seq``
    is monotonic across every sprint session the console run spawned; poll again with
    ``since=next_seq`` for the next page. ``complete`` is true once the console session
    reaches a terminal status, so a client knows no more events will arrive.
    """

    model_config = STRICT

    events: list[AgentEvent] = Field(default_factory=list)
    next_seq: int = 0
    complete: bool = False
