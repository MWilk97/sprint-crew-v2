"""The seam between a console session and the backlog run behind it.

Console status is derived from run status, never owned (AGENTS.md §4.2).
"""

from __future__ import annotations

import asyncio

from sprint_crew.api.console.clarify import (
    _user_text,
    all_clarification_lines,
    resolve_answer_text,
)
from sprint_crew.api.console.state import _TERMINAL_STATUSES, pending_review, touch
from sprint_crew.orchestrator.backlog import backlog_store, get_backlog_run
from sprint_crew.orchestrator.run_registry import run_registry
from sprint_crew.schemas.console import (
    ConsoleMessageRole,
    ConsolePlanResult,
    ConsoleSession,
    ConsoleSessionStatus,
    PlanPreviewStory,
)
from sprint_crew.schemas.session import BacklogRunStatus


async def sync_ask_state(session: ConsoleSession) -> None:
    """Recompute ``ask_in_flight`` from the registry (roadmap M9).

    Derived rather than owned, like ``awaiting_review``: a persisted boolean would survive
    a restart that killed the task, and a client would be left with a composer disabled
    forever by an answer that can never arrive. Not persisted here — the read path only
    needs the response to be right, and every writer touches the session anyway.
    """
    if session.active_ask_id is None:
        session.ask_in_flight = False
        return
    live = run_registry().get(session.active_ask_id) is not None
    session.ask_in_flight = live
    if not live:
        session.active_ask_id = None


def cancel_backlog_run(run_id: str) -> None:
    store = backlog_store()
    run = store.load(run_id)
    if run is None or run.status in (BacklogRunStatus.COMPLETED, BacklogRunStatus.FAILED):
        return
    store.save(
        run.model_copy(
            update={"status": BacklogRunStatus.CANCELLED, "error": run.error or "cancelled by user"}
        )
    )


def build_run_prompt(session: ConsoleSession) -> str:
    """Combine user messages, interpreter assumptions, and clarify answers for from-prompt."""
    parts = [_user_text(session)]
    # Assumptions the user saw and did not correct are decisions; ScrumMaster should not
    # rediscover them.
    if session.intent is not None and session.intent.assumptions:
        parts.append("Assumptions:\n" + "\n".join(f"- {a}" for a in session.intent.assumptions))
    # Every round, not just the open one: an answer given before the user sent another
    # message is still a decision the run has to honour (M9).
    lines = all_clarification_lines(session)
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
                title=f"Constraint: {resolve_answer_text(question, answer)}",
                rationale=question.text,
            )
        )
    return ConsolePlanResult(
        summary=f"Plan preview for: {first_prompt}",
        stories=stories,
    )


async def sync_sprint_progress(session: ConsoleSession) -> None:
    """Mirror run progress into a queued, running, or parked code-mode session."""
    if session.status not in (
        ConsoleSessionStatus.RUNNING,
        ConsoleSessionStatus.QUEUED,
        ConsoleSessionStatus.AWAITING_REVIEW,
    ):
        return
    if session.sprint_ref is None or session.sprint_ref.backlog_run_id is None:
        return
    run_id = session.sprint_ref.backlog_run_id
    # The registry is the authority on queue position: it holds the live task handles, while
    # the backlog row only says pending/running. Position becomes None on admission.
    position = run_registry().position(run_id)
    session.queue_position = position or None
    if not position and session.status is ConsoleSessionStatus.QUEUED:
        session.status = ConsoleSessionStatus.RUNNING
    # to_thread: get_backlog_run is a blocking SQLite read called from an async GET (M3).
    run = await asyncio.to_thread(get_backlog_run, run_id)
    if run is None:
        return
    session.sprint_ref.sprint_session_ids = list(run.session_ids)
    if run.status is BacklogRunStatus.COMPLETED:
        session.status = ConsoleSessionStatus.COMPLETED
    elif run.status is BacklogRunStatus.FAILED:
        session.status = ConsoleSessionStatus.FAILED
        session.error = run.error or "backlog run failed"
    elif run.status is BacklogRunStatus.CANCELLED:
        session.status = ConsoleSessionStatus.CANCELLED
    if session.status in (ConsoleSessionStatus.RUNNING, ConsoleSessionStatus.AWAITING_REVIEW):
        # Derived from the open review row rather than owned, like every other status here:
        # the run parks and unparks inside the graph, and only the store sees both (M7).
        parked = await pending_review(session.session_id) is not None
        session.status = (
            ConsoleSessionStatus.AWAITING_REVIEW if parked else ConsoleSessionStatus.RUNNING
        )
    if session.status in _TERMINAL_STATUSES:
        session.queue_position = None
    await touch(session)
