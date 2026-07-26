"""Live /v1/console/* routes (docs/contracts/chat-console-api.md, ADR 0011/0012).

MVP implementation notes:

- Sessions are durable in SQLite (``ConsoleSessionStore``) and survive a restart.
  Single-worker assumption still holds: the per-session asyncio locks below are
  process-local, so this is not safe across multiple API workers.
- Clarify questions come from the Interpreter on the Work lane (ADR 0013). When the
  lane is cold or the call fails, the deterministic stub below answers instead — an
  interactive caller must not block on a model load.
- mode=code start reuses the same orchestration as POST /sprint/from-prompt
  (``start_from_prompt_run`` in app.py); mode=plan never touches
  from-prompt/ship/Jira/git.
- Cancel of a running code-mode session marks the console session cancelled
  but does not stop the underlying backlog run (no kill support today).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from uuid import uuid4

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from sse_starlette import EventSourceResponse, ServerSentEvent

from sprint_crew.agents.interpreter import run_interpreter, to_clarify_questions
from sprint_crew.api.auth import require_token
from sprint_crew.config import Role, get_settings
from sprint_crew.graph.lanes import ensure_lane, lane_status
from sprint_crew.orchestrator.backlog import get_backlog_run
from sprint_crew.orchestrator.console_store import console_store, reap_console_sessions
from sprint_crew.orchestrator.event_bus import event_bus
from sprint_crew.orchestrator.event_log import EventLog, event_log
from sprint_crew.orchestrator.session import get_session
from sprint_crew.schemas.console import (
    ClarifyAnswer,
    ClarifyQuestion,
    ClarifyRequest,
    ClarifySuggestion,
    ConsoleEventsPage,
    ConsoleMessage,
    ConsoleMessageRole,
    ConsoleMode,
    ConsolePlanResult,
    ConsoleSession,
    ConsoleSessionStatus,
    CreateConsoleSessionRequest,
    IntentSummary,
    PlanPreviewStory,
    PostMessageRequest,
    SprintRunRef,
)
from sprint_crew.schemas.intent import IntentAnalysis
from sprint_crew.schemas.session import AgentEvent, BacklogRunStatus

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/v1/console",
    tags=["console"],
    dependencies=[Depends(require_token)],
)

_TERMINAL_STATUSES = frozenset(
    {
        ConsoleSessionStatus.COMPLETED,
        ConsoleSessionStatus.FAILED,
        ConsoleSessionStatus.CANCELLED,
    }
)

# One asyncio.Lock per session id, held across each handler's read-modify-write.
# threading.Lock did not protect across await points inside one event loop, so
# concurrent clarify + start could interleave. Entries are evicted when a session
# is reaped. Single-worker assumption: this dict is not shared across processes.
_locks: dict[str, asyncio.Lock] = {}


def _lock_for(session_id: str) -> asyncio.Lock:
    # Safe without its own guard: the event loop never preempts between these two
    # lines (no await), so two callers cannot create competing locks for one id.
    lock = _locks.get(session_id)
    if lock is None:
        lock = asyncio.Lock()
        _locks[session_id] = lock
    return lock


def reset_console_store() -> None:
    console_store().clear()
    event_log().clear()
    event_bus().clear()
    _locks.clear()


def run_console_reaper() -> list[str]:
    """Delete stale terminal sessions and evict their locks. Returns reaped ids."""
    reaped = reap_console_sessions()
    for session_id in reaped:
        _locks.pop(session_id, None)
    return reaped


def _utc_now_iso() -> str:
    return datetime.now(tz=UTC).isoformat()


def _touch(session: ConsoleSession) -> None:
    """Persist the session, stamping updated_at. The single save seam for every handler."""
    session.updated_at = _utc_now_iso()
    console_store().save(session)


def _get_session_or_404(session_id: str) -> ConsoleSession:
    session = console_store().load(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Console session not found")
    return session


def build_clarify_questions(prompt: str | None) -> list[ClarifyQuestion]:
    """Fallback clarify questions: fixed, lightly derived from the prompt, no LLM call."""
    text = (prompt or "").lower()
    questions = [
        ClarifyQuestion(
            question_id="q-scope",
            text="Which part of the repo should change?",
            suggestions=[
                ClarifySuggestion(
                    suggestion_id="s-scope-focused",
                    label="Only the files needed for this change",
                ),
                ClarifySuggestion(
                    suggestion_id="s-scope-broad",
                    label="Related modules too",
                    detail="including tests and docs touched by the change",
                ),
            ],
            allow_custom=True,
        ),
        ClarifyQuestion(
            question_id="q-tests",
            text="What test coverage do you expect?",
            suggestions=[
                ClarifySuggestion(suggestion_id="s-tests-unit", label="Unit tests only"),
                ClarifySuggestion(suggestion_id="s-tests-full", label="Unit + integration tests"),
            ],
            allow_custom=True,
        ),
    ]
    if any(keyword in text for keyword in ("api", "endpoint", "route")):
        questions.append(
            ClarifyQuestion(
                question_id="q-compat",
                text="Should existing API behavior stay unchanged?",
                suggestions=[
                    ClarifySuggestion(
                        suggestion_id="s-compat-keep", label="Yes, additive change only"
                    ),
                    ClarifySuggestion(
                        suggestion_id="s-compat-break", label="Breaking changes acceptable"
                    ),
                ],
                allow_custom=True,
            )
        )
    return questions


def _user_text(session: ConsoleSession) -> str:
    return "\n".join(m.content for m in session.messages if m.role is ConsoleMessageRole.USER)


async def _work_lane_available() -> bool:
    """True when the Interpreter can run now without making the caller wait on a load."""
    if get_settings().clarify_autostart_lane:
        await ensure_lane(Role.WORK)
        return True
    return await asyncio.to_thread(lane_status, Role.WORK) == "ok"


async def _analyze(session: ConsoleSession, prompt: str) -> IntentAnalysis | None:
    """Interpreter output, or None when clarify must fall back to the stub."""
    if not get_settings().clarify_llm_enabled or not prompt.strip():
        return None
    try:
        if not await _work_lane_available():
            logger.info("console clarify: work lane not ready, using deterministic questions")
            return None
        return await run_interpreter(user_prompt=prompt, project_hint=session.repo_url or "")
    except Exception:
        logger.exception("console clarify: interpreter failed, using deterministic questions")
        return None


async def _interpret(session: ConsoleSession) -> tuple[list[ClarifyQuestion], IntentSummary | None]:
    """LLM clarify, degrading to the deterministic stub instead of failing the request."""
    prompt = _user_text(session)
    analysis = await _analyze(session, prompt)
    if analysis is None:
        return build_clarify_questions(prompt), None
    questions = to_clarify_questions(analysis, limit=get_settings().max_clarify_questions)
    return questions, IntentSummary.model_validate(analysis, from_attributes=True)


async def _enter_clarifying(session: ConsoleSession) -> None:
    questions, intent = await _interpret(session)
    session.clarify_questions = questions
    session.intent = intent
    if questions:
        content = "Before starting, please answer the clarify questions."
        session.status = ConsoleSessionStatus.CLARIFYING
    else:
        # Nothing ambiguous is worth interrupting for; confirm still gates the run.
        understood = f"Understood: {intent.restated_goal}. " if intent is not None else ""
        content = f"{understood}No open questions — confirm to start."
        session.status = ConsoleSessionStatus.READY
    session.messages.append(ConsoleMessage(role=ConsoleMessageRole.ASSISTANT, content=content))


def apply_clarify_answers(session: ConsoleSession, answers: list[ClarifyAnswer]) -> None:
    """Validate and record answers; moves the session to ready once all are answered.

    Raises ValueError for contract-level 400s (unknown question, duplicate answer,
    custom answer where allow_custom is false, unknown suggestion id).
    """
    questions = {q.question_id: q for q in session.clarify_questions}
    answered = {a.question_id for a in session.clarify_answers}
    for answer in answers:
        question = questions.get(answer.question_id)
        if question is None:
            raise ValueError(f"unknown question_id: {answer.question_id}")
        if answer.question_id in answered:
            raise ValueError(f"question already answered: {answer.question_id}")
        if answer.custom_text is not None and not question.allow_custom:
            raise ValueError(f"question {answer.question_id} does not allow a custom answer")
        if answer.selected_suggestion_id is not None and answer.selected_suggestion_id not in {
            s.suggestion_id for s in question.suggestions
        }:
            raise ValueError(
                f"unknown suggestion_id for {answer.question_id}: {answer.selected_suggestion_id}"
            )
        answered.add(answer.question_id)
    session.clarify_answers.extend(answers)
    if answered >= set(questions):
        session.status = ConsoleSessionStatus.READY


def _resolve_answer_text(question: ClarifyQuestion, answer: ClarifyAnswer) -> str:
    if answer.custom_text is not None:
        return answer.custom_text
    for suggestion in question.suggestions:
        if suggestion.suggestion_id == answer.selected_suggestion_id:
            return suggestion.label
    return answer.selected_suggestion_id or ""


def _clarification_lines(session: ConsoleSession) -> list[str]:
    questions = {q.question_id: q for q in session.clarify_questions}
    return [
        f"- {questions[a.question_id].text} {_resolve_answer_text(questions[a.question_id], a)}"
        for a in session.clarify_answers
        if a.question_id in questions
    ]


def build_run_prompt(session: ConsoleSession) -> str:
    """Combine user messages, interpreter assumptions, and clarify answers for from-prompt."""
    parts = [_user_text(session)]
    # Assumptions the user saw and did not correct are decisions; ScrumMaster should not
    # rediscover them.
    if session.intent is not None and session.intent.assumptions:
        parts.append("Assumptions:\n" + "\n".join(f"- {a}" for a in session.intent.assumptions))
    lines = _clarification_lines(session)
    if lines:
        parts.append("Clarifications:\n" + "\n".join(lines))
    return "\n\n".join(parts)


def build_plan_result(session: ConsoleSession) -> ConsolePlanResult:
    """Heuristic plan-mode stub: preview stories from the prompt and clarify answers."""
    first_prompt = next(
        (m.content for m in session.messages if m.role is ConsoleMessageRole.USER),
        "the request",
    )
    stories = [
        PlanPreviewStory(
            title=f"Implement: {first_prompt}",
            rationale="core change requested by the user",
        )
    ]
    questions = {q.question_id: q for q in session.clarify_questions}
    for answer in session.clarify_answers:
        question = questions.get(answer.question_id)
        if question is None:
            continue
        stories.append(
            PlanPreviewStory(
                title=f"Constraint: {_resolve_answer_text(question, answer)}",
                rationale=question.text,
            )
        )
    return ConsolePlanResult(
        summary=f"Plan preview for: {first_prompt}",
        stories=stories,
    )


async def _sync_sprint_progress(session: ConsoleSession) -> None:
    """Mirror backlog run progress into a running code-mode session."""
    if session.status is not ConsoleSessionStatus.RUNNING:
        return
    if session.sprint_ref is None or session.sprint_ref.backlog_run_id is None:
        return
    # to_thread: get_backlog_run is a blocking SQLite read called from an async GET (M3).
    run = await asyncio.to_thread(get_backlog_run, session.sprint_ref.backlog_run_id)
    if run is None:
        return
    session.sprint_ref.sprint_session_ids = list(run.session_ids)
    if run.status is BacklogRunStatus.COMPLETED:
        session.status = ConsoleSessionStatus.COMPLETED
    elif run.status is BacklogRunStatus.FAILED:
        session.status = ConsoleSessionStatus.FAILED
        session.error = run.error or "backlog run failed"
    _touch(session)


@router.post("/sessions", response_model=ConsoleSession, status_code=201)
async def create_console_session(body: CreateConsoleSessionRequest) -> ConsoleSession:
    session = ConsoleSession(
        session_id=f"cs-{uuid4().hex[:8]}",
        mode=body.mode,
        status=ConsoleSessionStatus.COLLECTING,
        repo_url=body.repo_url,
        target_language=body.target_language,
    )
    async with _lock_for(session.session_id):
        if body.initial_prompt:
            session.messages.append(
                ConsoleMessage(role=ConsoleMessageRole.USER, content=body.initial_prompt)
            )
            await _enter_clarifying(session)
        _touch(session)
    return session


@router.get("/sessions/{id}", response_model=ConsoleSession)
async def get_console_session(id: str) -> ConsoleSession:
    async with _lock_for(id):
        session = _get_session_or_404(id)
        await _sync_sprint_progress(session)
        if session.status in _TERMINAL_STATUSES:
            run_console_reaper()
        return session


@router.post("/sessions/{id}/messages", response_model=ConsoleSession)
async def post_console_message(id: str, body: PostMessageRequest) -> ConsoleSession:
    async with _lock_for(id):
        session = _get_session_or_404(id)
        if session.status is ConsoleSessionStatus.RUNNING or session.status in _TERMINAL_STATUSES:
            raise HTTPException(
                status_code=409,
                detail=f"session is {session.status.value}; no further messages accepted",
            )
        session.messages.append(ConsoleMessage(role=ConsoleMessageRole.USER, content=body.content))
        if session.status is ConsoleSessionStatus.COLLECTING:
            await _enter_clarifying(session)
        _touch(session)
        return session


@router.post("/sessions/{id}/clarify", response_model=ConsoleSession)
async def submit_clarify_answers(id: str, body: ClarifyRequest) -> ConsoleSession:
    async with _lock_for(id):
        session = _get_session_or_404(id)
        if session.status is not ConsoleSessionStatus.CLARIFYING:
            raise HTTPException(
                status_code=409,
                detail=f"session is {session.status.value}, not awaiting clarification",
            )
        try:
            apply_clarify_answers(session, body.answers)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        _touch(session)
        return session


@router.post("/sessions/{id}/confirm", response_model=ConsoleSession)
async def confirm_console_session(id: str) -> ConsoleSession:
    async with _lock_for(id):
        session = _get_session_or_404(id)
        if session.status is not ConsoleSessionStatus.READY:
            raise HTTPException(
                status_code=409,
                detail=f"session is {session.status.value}; confirm requires ready",
            )
        session.confirmed = True
        _touch(session)
        return session


@router.post("/sessions/{id}/start", response_model=ConsoleSession)
async def start_console_run(id: str, background_tasks: BackgroundTasks) -> ConsoleSession:
    async with _lock_for(id):
        session = _get_session_or_404(id)
        if session.status is not ConsoleSessionStatus.READY:
            raise HTTPException(
                status_code=409,
                detail=f"session is {session.status.value}; start requires ready",
            )
        if not session.confirmed:
            raise HTTPException(status_code=409, detail="session must be confirmed before start")

        if session.mode is ConsoleMode.PLAN:
            # Plan mode never ships: no from-prompt run, no Jira, no git writes (ADR 0012).
            session.plan_result = build_plan_result(session)
            session.status = ConsoleSessionStatus.COMPLETED
            _touch(session)
            run_console_reaper()
            return session

        # Lazy import: app.py imports this router at module load.
        from sprint_crew.api.app import start_from_prompt_run

        try:
            run_id = await start_from_prompt_run(
                prompt=build_run_prompt(session),
                repo_url=session.repo_url,
                background_tasks=background_tasks,
                console_session_id=session.session_id,
            )
        except Exception as exc:
            session.status = ConsoleSessionStatus.FAILED
            session.error = str(exc)
            _touch(session)
            raise
        session.sprint_ref = SprintRunRef(backlog_run_id=run_id)
        session.status = ConsoleSessionStatus.RUNNING
        _touch(session)
        return session


def _backfill_events(session: ConsoleSession, log: EventLog) -> None:
    """Project a legacy session's sprint-session events into the events table.

    Sessions that ran before the events table existed have events only inside their
    sprint sessions. Concatenate them in sprint-session order (stories run
    sequentially, so this preserves chronology) so the timeline endpoint can still
    render them. Runs live-appending events never reach here — ``has_events`` is
    already true for them.
    """
    if session.sprint_ref is None:
        return
    for sprint_session_id in session.sprint_ref.sprint_session_ids:
        sprint_session = get_session(sprint_session_id)
        if sprint_session is None or not sprint_session.events:
            continue
        log.append_many(session.session_id, sprint_session_id, sprint_session.events)


@router.get("/sessions/{id}/events", response_model=ConsoleEventsPage)
async def get_console_events(id: str, since: int = 0, limit: int = 500) -> ConsoleEventsPage:
    """The console timeline, served by polling (SSE transport arrives in M3).

    ``seq`` is monotonic across every sprint session this console run spawned; poll
    again with ``since=next_seq`` to drain the next page. ``complete`` reports whether
    the session reached a terminal status, not whether this page is the last — a client
    keeps polling until it receives an empty page while ``complete`` is true.
    """
    limit = max(1, min(limit, 1000))
    async with _lock_for(id):
        session = _get_session_or_404(id)
        log = event_log()
        if since <= 0 and not log.has_events(id):
            _backfill_events(session, log)
        events = log.read(id, since=since, limit=limit)
        next_seq = events[-1].seq if events and events[-1].seq is not None else since
        complete = session.status in _TERMINAL_STATUSES
        return ConsoleEventsPage(events=events, next_seq=next_seq, complete=complete)


def _sse_event(event: AgentEvent) -> ServerSentEvent:
    """One timeline event as an SSE frame. ``id`` = ``seq`` so native EventSource resume
    works; the payload is the full AgentEvent JSON (same shape the polling endpoint serves),
    left on the default ``message`` channel so a plain ``onmessage`` handler receives every
    event and reads ``event_type`` from the body."""
    return ServerSentEvent(
        id=str(event.seq) if event.seq is not None else None,
        data=event.model_dump_json(),
    )


async def _stream_is_complete(session_id: str) -> bool:
    """Read-only terminal check for the SSE loop: true once no more events can be produced.

    The console session's stored status only flips terminal when someone GETs it
    (``_sync_sprint_progress``), so consult the backlog run directly rather than trusting the
    stored status alone. A reaped/missing session also counts as complete so the stream ends.
    """
    session = await asyncio.to_thread(console_store().load, session_id)
    if session is None or session.status in _TERMINAL_STATUSES:
        return True
    ref = session.sprint_ref
    if ref is not None and ref.backlog_run_id is not None:
        run = await asyncio.to_thread(get_backlog_run, ref.backlog_run_id)
        if run is not None and run.status in (
            BacklogRunStatus.COMPLETED,
            BacklogRunStatus.FAILED,
        ):
            return True
    return False


@router.get("/sessions/{id}/stream")
async def stream_console_events(id: str, request: Request, since: int = 0) -> EventSourceResponse:
    """Live SSE timeline (M3) — same payload as the polling events endpoint, pushed.

    Resume: the browser sends ``Last-Event-ID`` automatically on reconnect (or pass
    ``?since=``); we replay from the events table up to the current tail, then stream new
    events off the in-process bus. Subscribing *before* the replay closes the handoff gap —
    events appended mid-replay land in the queue and are de-duplicated by ``seq``. A heartbeat
    comment every ``SSE_HEARTBEAT_S`` survives proxy idle-reap and long GPU silences; a
    terminal run ends with an explicit ``event: done`` so the client stops reconnecting.

    Auth: browser ``EventSource`` cannot set headers, so ``require_token`` also accepts
    ``?token=`` (see api/auth.py).
    """
    session = await asyncio.to_thread(console_store().load, id)
    if session is None:
        raise HTTPException(status_code=404, detail="Console session not found")

    last_event_id = request.headers.get("last-event-id")
    if last_event_id is not None:
        try:
            since = int(last_event_id)
        except ValueError:
            pass
    cursor = max(since, 0)

    log = event_log()
    if cursor <= 0 and not await asyncio.to_thread(log.has_events, id):
        async with _lock_for(id):
            fresh = _get_session_or_404(id)
            if not log.has_events(id):
                await asyncio.to_thread(_backfill_events, fresh, log)

    heartbeat = get_settings().sse_heartbeat_s
    bus = event_bus()
    queue = bus.subscribe(id)

    async def _events() -> AsyncIterator[ServerSentEvent]:
        nonlocal cursor
        try:
            while True:
                batch = await asyncio.to_thread(log.read, id, since=cursor, limit=500)
                if not batch:
                    break
                for event in batch:
                    yield _sse_event(event)
                    cursor = event.seq or cursor
            while True:
                # The timeout is the terminal-check cadence, not the heartbeat: keepalive
                # pings are emitted by EventSourceResponse(ping=...) on its own task, so a
                # quiet run is kept alive without this generator having to yield anything.
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=heartbeat)
                except TimeoutError:
                    tail = await asyncio.to_thread(log.max_seq, id)
                    if cursor >= tail and await _stream_is_complete(id):
                        yield ServerSentEvent(event="done", data="")
                        return
                    continue
                if event.seq is None or event.seq <= cursor:
                    continue
                yield _sse_event(event)
                cursor = event.seq
        finally:
            bus.unsubscribe(id, queue)

    return EventSourceResponse(_events(), ping=max(1, int(heartbeat)))


@router.post("/sessions/{id}/cancel", response_model=ConsoleSession)
async def cancel_console_session(id: str) -> ConsoleSession:
    async with _lock_for(id):
        session = _get_session_or_404(id)
        if session.status in _TERMINAL_STATUSES:
            raise HTTPException(status_code=409, detail=f"session already {session.status.value}")
        # Best effort: a running backlog run is not killed, only the console session
        # is marked cancelled (documented limitation).
        session.status = ConsoleSessionStatus.CANCELLED
        _touch(session)
        run_console_reaper()
        return session
