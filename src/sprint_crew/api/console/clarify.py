"""Clarify: Interpreter integration, deterministic fallback, answer validation.

No FastAPI here — the routes call in, so this stays unit-testable without HTTP. It is the
part of the console most likely to keep changing (ADR 0013).

**Rounds (roadmap M9).** A message sent while clarifying or ready re-runs the Interpreter,
which *replaces* the open questions rather than adding to them. Question ids are therefore
round-scoped (``q-{round}-{n}``): without that, round 2's first question would silently
inherit round 1's answer, since both would be ``q-1``. Answers from a retired round are kept
as Interpreter context — they are still things the user told us — but they stop being answers,
because the questions they answered are no longer being asked.

**Grounding (roadmap M9).** M8 gave the session a checkout at creation; this is what consumes
it. The Interpreter's own prompt has always said "never ask what you can look up in the
repository context", an instruction it could not obey while the context was empty. Grounding
is best-effort in every direction: no workspace, a failed gather, or a cold lane all degrade
to the same deterministic stub, because clarify degrades and never blocks (ADR 0013).
"""

from __future__ import annotations

import asyncio
import logging
import re
from pathlib import Path

from sprint_crew.agents.interpreter import run_interpreter, to_clarify_questions
from sprint_crew.config import Role, get_settings
from sprint_crew.graph.lanes import ensure_lane, lane_status
from sprint_crew.orchestrator.repo_context import enrich_repo_context
from sprint_crew.schemas.console import (
    ClarifyAnswer,
    ClarifyQuestion,
    ClarifySuggestion,
    ConsoleMessage,
    ConsoleMessageRole,
    ConsoleSession,
    ConsoleSessionStatus,
    IntentSummary,
    WorkspaceStatus,
)
from sprint_crew.schemas.intent import IntentAnalysis
from sprint_crew.vector.scope import index_scope_for

logger = logging.getLogger(__name__)

#: ``q-{round}-…`` — used to tell "you are answering a retired round" apart from "that
#: question id never existed", which are a 409 and a 400 respectively.
_ROUND_RE = re.compile(r"^q-(\d+)-")

#: How much of the conversation the Interpreter is re-shown on a later round.
_CONVERSATION_CHARS = 3000


class StaleClarifyRoundError(ValueError):
    """An answer addressed a question set that has since been replaced."""


def build_clarify_questions(prompt: str | None, *, round_no: int = 1) -> list[ClarifyQuestion]:
    """Fallback clarify questions: fixed, lightly derived from the prompt, no LLM call."""
    text = (prompt or "").lower()
    questions = [
        ClarifyQuestion(
            question_id=f"q-{round_no}-scope",
            text="Which part of the repo should change?",
            suggestions=[
                ClarifySuggestion(
                    suggestion_id=f"s-{round_no}-scope-focused",
                    label="Only the files needed for this change",
                ),
                ClarifySuggestion(
                    suggestion_id=f"s-{round_no}-scope-broad",
                    label="Related modules too",
                    detail="including tests and docs touched by the change",
                ),
            ],
            allow_custom=True,
        ),
        ClarifyQuestion(
            question_id=f"q-{round_no}-tests",
            text="What test coverage do you expect?",
            suggestions=[
                ClarifySuggestion(
                    suggestion_id=f"s-{round_no}-tests-unit", label="Unit tests only"
                ),
                ClarifySuggestion(
                    suggestion_id=f"s-{round_no}-tests-full", label="Unit + integration tests"
                ),
            ],
            allow_custom=True,
        ),
    ]
    if any(keyword in text for keyword in ("api", "endpoint", "route")):
        questions.append(
            ClarifyQuestion(
                question_id=f"q-{round_no}-compat",
                text="Should existing API behavior stay unchanged?",
                suggestions=[
                    ClarifySuggestion(
                        suggestion_id=f"s-{round_no}-compat-keep",
                        label="Yes, additive change only",
                    ),
                    ClarifySuggestion(
                        suggestion_id=f"s-{round_no}-compat-break",
                        label="Breaking changes acceptable",
                    ),
                ],
                allow_custom=True,
            )
        )
    return questions


def _user_text(session: ConsoleSession) -> str:
    return "\n".join(m.content for m in session.messages if m.role is ConsoleMessageRole.USER)


def _conversation_text(session: ConsoleSession) -> str:
    """What the Interpreter is shown besides the request itself, on a later round.

    Assistant turns and every clarification the user has already given, including retired
    rounds — the point of re-interpreting is to not ask the same thing twice.
    """
    parts: list[str] = []
    lines = all_clarification_lines(session)
    if lines:
        parts.append("Already clarified:\n" + "\n".join(lines))
    assistant = [m.content for m in session.messages if m.role is ConsoleMessageRole.ASSISTANT]
    if assistant:
        parts.append("Earlier replies:\n" + "\n".join(f"- {a}" for a in assistant[-4:]))
    text = "\n\n".join(parts)
    return text[-_CONVERSATION_CHARS:] if len(text) > _CONVERSATION_CHARS else text


def _gather_repo_context(root: Path, query: str) -> str:
    # Shared tier only: the session's checkout is the repository's committed state, and no
    # run overlay exists for it.
    return enrich_repo_context(root, index_scope_for(root), query)


async def repo_context_for(session: ConsoleSession, query: str) -> str:
    """The session checkout's context for grounding, or "" when there is none to be had."""
    settings = get_settings()
    if not settings.clarify_repo_context_enabled:
        return ""
    if session.workspace_status is not WorkspaceStatus.READY or not session.workspace_root:
        return ""
    root = Path(session.workspace_root)
    try:
        if not await asyncio.to_thread(root.is_dir):
            return ""
        # to_thread: this is git subprocesses, a tree walk, a grep and a Qdrant round-trip,
        # all blocking, all called from an async handler (M3).
        text = await asyncio.to_thread(_gather_repo_context, root, query)
    except Exception:
        logger.warning("console clarify: repo context unavailable", exc_info=True)
        return ""
    cap = settings.interpreter_repo_context_chars
    if len(text) > cap:
        text = text[:cap] + "\n… (repository context truncated)"
    return text


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
        # Gathered before the call, not inside it: a cold lane must not have paid for a repo
        # walk it is about to discard.
        repo_context = await repo_context_for(session, prompt)
        return await run_interpreter(
            user_prompt=prompt,
            repo_context=repo_context,
            conversation=_conversation_text(session),
            project_hint=session.repo_url or "",
        )
    except Exception:
        logger.exception("console clarify: interpreter failed, using deterministic questions")
        return None


async def _interpret(session: ConsoleSession) -> tuple[list[ClarifyQuestion], IntentSummary | None]:
    """LLM clarify, degrading to the deterministic stub instead of failing the request."""
    prompt = _user_text(session)
    analysis = await _analyze(session, prompt)
    if analysis is None:
        return build_clarify_questions(prompt, round_no=session.clarify_round), None
    questions = to_clarify_questions(
        analysis,
        limit=get_settings().max_clarify_questions,
        round_no=session.clarify_round,
    )
    return questions, IntentSummary.model_validate(analysis, from_attributes=True)


def roll_clarify_round(session: ConsoleSession) -> bool:
    """Retire the open question set so a fresh one can replace it. True if a round was retired.

    Keyed on the status the session arrives in, not on whether it has questions: an
    interpretation that produced *zero* questions still produced an understanding, and
    re-interpreting has to supersede it. Sniffing at the question list instead let a
    confirmed zero-question session keep its confirmation across a re-interpretation.

    Answers survive as prose in ``prior_clarifications`` — the user did tell us those things,
    and the next round must not ask again — but they stop being answers, because the
    questions they were answers to no longer exist.
    """
    if session.status not in (ConsoleSessionStatus.CLARIFYING, ConsoleSessionStatus.READY):
        return False
    session.prior_clarifications.extend(clarification_lines(session))
    session.clarify_questions = []
    session.clarify_answers = []
    session.clarify_round += 1
    return True


async def enter_clarifying(session: ConsoleSession) -> bool:
    """Re-interpret the conversation and publish a question set. True if a round was retired."""
    rolled = roll_clarify_round(session)
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
    if rolled:
        # A confirmation belongs to the understanding it was given for. Re-interpreting
        # produced a different one, so it has to be given again.
        session.confirmed = False
    session.messages.append(ConsoleMessage(role=ConsoleMessageRole.ASSISTANT, content=content))
    return rolled


def apply_clarify_answers(session: ConsoleSession, answers: list[ClarifyAnswer]) -> None:
    """Validate and record answers; moves the session to ready once all are answered.

    Raises ValueError for contract-level 400s (unknown question, duplicate answer,
    custom answer where allow_custom is false, unknown suggestion id), and the
    StaleClarifyRoundError subclass — a 409 — when the question set has moved on.
    """
    questions = {q.question_id: q for q in session.clarify_questions}
    answered = {a.question_id for a in session.clarify_answers}
    for answer in answers:
        question = questions.get(answer.question_id)
        if question is None:
            match = _ROUND_RE.match(answer.question_id)
            if match is not None and int(match.group(1)) != session.clarify_round:
                raise StaleClarifyRoundError(
                    f"{answer.question_id} belongs to clarify round {match.group(1)}; "
                    f"round {session.clarify_round} is open — refetch the session"
                )
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


def resolve_answer_text(question: ClarifyQuestion, answer: ClarifyAnswer) -> str:
    if answer.custom_text is not None:
        return answer.custom_text
    for suggestion in question.suggestions:
        if suggestion.suggestion_id == answer.selected_suggestion_id:
            return suggestion.label
    return answer.selected_suggestion_id or ""


def clarification_lines(session: ConsoleSession) -> list[str]:
    """Resolved Q&A for the *open* round only."""
    questions = {q.question_id: q for q in session.clarify_questions}
    return [
        f"- {questions[a.question_id].text} {resolve_answer_text(questions[a.question_id], a)}"
        for a in session.clarify_answers
        if a.question_id in questions
    ]


def all_clarification_lines(session: ConsoleSession) -> list[str]:
    """Everything the user has clarified, retired rounds included.

    What a run prompt and a re-interpretation both want: an answer given in round 1 is still
    a decision the user made, even though the question that elicited it is gone.
    """
    return [*session.prior_clarifications, *clarification_lines(session)]
