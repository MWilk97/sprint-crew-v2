"""Clarify: Interpreter integration, deterministic fallback, answer validation.

No FastAPI here — the routes call in, so this stays unit-testable without HTTP. It is the
part of the console most likely to keep changing (ADR 0013).
"""

from __future__ import annotations

import asyncio
import logging

from sprint_crew.agents.interpreter import run_interpreter, to_clarify_questions
from sprint_crew.config import Role, get_settings
from sprint_crew.graph.lanes import ensure_lane, lane_status
from sprint_crew.schemas.console import (
    ClarifyAnswer,
    ClarifyQuestion,
    ClarifySuggestion,
    ConsoleMessage,
    ConsoleMessageRole,
    ConsoleSession,
    ConsoleSessionStatus,
    IntentSummary,
)
from sprint_crew.schemas.intent import IntentAnalysis

logger = logging.getLogger(__name__)


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


async def enter_clarifying(session: ConsoleSession) -> None:
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


def resolve_answer_text(question: ClarifyQuestion, answer: ClarifyAnswer) -> str:
    if answer.custom_text is not None:
        return answer.custom_text
    for suggestion in question.suggestions:
        if suggestion.suggestion_id == answer.selected_suggestion_id:
            return suggestion.label
    return answer.selected_suggestion_id or ""


def clarification_lines(session: ConsoleSession) -> list[str]:
    questions = {q.question_id: q for q in session.clarify_questions}
    return [
        f"- {questions[a.question_id].text} {resolve_answer_text(questions[a.question_id], a)}"
        for a in session.clarify_answers
        if a.question_id in questions
    ]
