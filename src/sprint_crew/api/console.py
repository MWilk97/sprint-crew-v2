"""Live /v1/console/* routes (docs/contracts/chat-console-api.md, ADR 0011/0012).

MVP implementation notes:

- Sessions live in a process-local in-memory dict — they do not survive a
  restart and are not shared across API workers (single-worker assumption).
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
import threading
from datetime import UTC, datetime
from uuid import uuid4

from fastapi import APIRouter, BackgroundTasks, HTTPException

from sprint_crew.agents.interpreter import run_interpreter, to_clarify_questions
from sprint_crew.config import Role, get_settings
from sprint_crew.graph.lanes import ensure_lane, lane_status
from sprint_crew.orchestrator.backlog import get_backlog_run
from sprint_crew.schemas.console import (
    ClarifyAnswer,
    ClarifyQuestion,
    ClarifyRequest,
    ClarifySuggestion,
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
from sprint_crew.schemas.session import BacklogRunStatus

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/console", tags=["console"])

_TERMINAL_STATUSES = frozenset(
    {
        ConsoleSessionStatus.COMPLETED,
        ConsoleSessionStatus.FAILED,
        ConsoleSessionStatus.CANCELLED,
    }
)

_sessions: dict[str, ConsoleSession] = {}
_sessions_lock = threading.Lock()


def reset_console_store() -> None:
    with _sessions_lock:
        _sessions.clear()


def _utc_now_iso() -> str:
    return datetime.now(tz=UTC).isoformat()


def _touch(session: ConsoleSession) -> None:
    session.updated_at = _utc_now_iso()


def _get_session_or_404(session_id: str) -> ConsoleSession:
    with _sessions_lock:
        session = _sessions.get(session_id)
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
    session.messages.append(
        ConsoleMessage(role=ConsoleMessageRole.ASSISTANT, content=content)
    )


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


def _sync_sprint_progress(session: ConsoleSession) -> None:
    """Mirror backlog run progress into a running code-mode session."""
    if session.status is not ConsoleSessionStatus.RUNNING:
        return
    if session.sprint_ref is None or session.sprint_ref.backlog_run_id is None:
        return
    run = get_backlog_run(session.sprint_ref.backlog_run_id)
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
    if body.initial_prompt:
        session.messages.append(
            ConsoleMessage(role=ConsoleMessageRole.USER, content=body.initial_prompt)
        )
        await _enter_clarifying(session)
    with _sessions_lock:
        _sessions[session.session_id] = session
    return session


@router.get("/sessions/{id}", response_model=ConsoleSession)
async def get_console_session(id: str) -> ConsoleSession:
    session = _get_session_or_404(id)
    _sync_sprint_progress(session)
    return session


@router.post("/sessions/{id}/messages", response_model=ConsoleSession)
async def post_console_message(id: str, body: PostMessageRequest) -> ConsoleSession:
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
        return session

    # Lazy import: app.py imports this router at module load.
    from sprint_crew.api.app import start_from_prompt_run

    try:
        run_id = await start_from_prompt_run(
            prompt=build_run_prompt(session),
            repo_url=session.repo_url,
            background_tasks=background_tasks,
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


@router.post("/sessions/{id}/cancel", response_model=ConsoleSession)
async def cancel_console_session(id: str) -> ConsoleSession:
    session = _get_session_or_404(id)
    if session.status in _TERMINAL_STATUSES:
        raise HTTPException(status_code=409, detail=f"session already {session.status.value}")
    # Best effort: a running backlog run is not killed, only the console session
    # is marked cancelled (documented limitation).
    session.status = ConsoleSessionStatus.CANCELLED
    _touch(session)
    return session
