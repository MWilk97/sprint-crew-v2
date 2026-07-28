"""Session lifecycle: create, read, message, clarify, confirm, start, cancel.

Thin orchestration over state/clarify/run_bridge, so this file reads as the API contract.
The two timeline routes live in ``events.py`` with the transport they belong to.
"""

from __future__ import annotations

import asyncio
from uuid import uuid4

from fastapi import HTTPException

from sprint_crew.api.console.clarify import (
    StaleClarifyRoundError,
    apply_clarify_answers,
    enter_clarifying,
)
from sprint_crew.api.console.plan import resolve_depth, submit_plan_run
from sprint_crew.api.console.run_bridge import (
    build_run_prompt,
    cancel_backlog_run,
    sync_ask_state,
    sync_plan_progress,
    sync_sprint_progress,
)
from sprint_crew.api.console.state import (
    _STARTED_STATUSES,
    _TERMINAL_STATUSES,
    _lock_for,
    _utc_now_iso,
    arun_console_reaper,
    close_review,
    drop_lock,
    emit,
    list_sessions_page,
    pending_review,
    purge_session,
    require_session,
    router,
    touch,
)
from sprint_crew.api.console.workspace import schedule_workspace_prep
from sprint_crew.orchestrator.run_registry import run_registry
from sprint_crew.schemas.console import (
    ClarifyRequest,
    ConsoleMessage,
    ConsoleMessageRole,
    ConsoleMode,
    ConsoleSession,
    ConsoleSessionPage,
    ConsoleSessionStatus,
    ConsoleSessionSummary,
    CreateConsoleSessionRequest,
    PostMessageRequest,
    SprintRunRef,
    StartRunRequest,
)
from sprint_crew.schemas.session import agent_event

# How much of the opening message becomes a session's title in the history list.
_TITLE_CHARS = 120


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
        # Before clarify, not after: the clone runs while the Interpreter thinks (15-120 s),
        # instead of starting once it is done.
        schedule_workspace_prep(session)
        if body.initial_prompt:
            session.messages.append(
                ConsoleMessage(role=ConsoleMessageRole.USER, content=body.initial_prompt)
            )
            await enter_clarifying(session)
        await touch(session)
    return session


@router.get("/sessions", response_model=ConsoleSessionPage)
async def list_console_sessions(limit: int = 25, offset: int = 0) -> ConsoleSessionPage:
    """Session history, newest first. Summaries only — see `GET /sessions/{id}` for one."""
    limit = max(1, min(limit, 100))
    offset = max(0, offset)
    sessions, total = await list_sessions_page(limit=limit, offset=offset)
    return ConsoleSessionPage(
        sessions=[_summarize(s) for s in sessions],
        total=total,
        next_offset=offset + limit if offset + limit < total else None,
    )


def _summarize(session: ConsoleSession) -> ConsoleSessionSummary:
    first_user = next(
        (m.content for m in session.messages if m.role is ConsoleMessageRole.USER),
        None,
    )
    return ConsoleSessionSummary(
        session_id=session.session_id,
        mode=session.mode,
        status=session.status,
        workspace_status=session.workspace_status,
        title=first_user[:_TITLE_CHARS] if first_user else None,
        repo_url=session.repo_url,
        parent_session_id=session.parent_session_id,
        created_at=session.created_at,
        updated_at=session.updated_at,
    )


@router.delete("/sessions/{id}", status_code=204)
async def delete_console_session(id: str) -> None:
    """Delete a session and everything it owns: row, clone, diffs, timeline.

    Refuses while a run is live rather than cancelling implicitly — deleting the session
    under a running agent would leave the run writing into a workspace nobody owns.
    """
    await require_session(id)
    async with _lock_for(id):
        session = await require_session(id)
        if session.status in _STARTED_STATUSES:
            raise HTTPException(
                status_code=409,
                detail=f"session is {session.status.value}; cancel it before deleting",
            )
        await sync_ask_state(session)
        if session.ask_in_flight:
            # Same reason as a live run: purging the workspace under the Explainer would
            # leave it reading files out of a directory nobody owns.
            raise HTTPException(
                status_code=409, detail="an answer is being generated; cancel the ask first"
            )
        await purge_session(session)
    drop_lock(id)


@router.get("/sessions/{id}", response_model=ConsoleSession)
async def get_console_session(id: str) -> ConsoleSession:
    await require_session(id)
    async with _lock_for(id):
        session = await require_session(id)
        await sync_sprint_progress(session)
        await sync_plan_progress(session)
        await sync_ask_state(session)
        if session.status in _TERMINAL_STATUSES:
            await arun_console_reaper()
        return session


@router.post("/sessions/{id}/messages", response_model=ConsoleSession)
async def post_console_message(id: str, body: PostMessageRequest) -> ConsoleSession:
    """Add a message and re-interpret. No longer inert outside ``collecting`` (roadmap M9).

    A message sent while clarifying or ready used to be appended and silently ignored, which
    made the composer a lie in the two states a user is most likely to want it. It now
    re-runs the Interpreter over the whole conversation, which *replaces* the open question
    set — see ``clarify.roll_clarify_round``. ``ready`` can therefore go back to
    ``clarifying``, the first backwards transition in this state machine.
    """
    await require_session(id)
    async with _lock_for(id):
        session = await require_session(id)
        if session.status in _STARTED_STATUSES or session.status in _TERMINAL_STATUSES:
            raise HTTPException(
                status_code=409,
                detail=f"session is {session.status.value}; no further messages accepted",
            )
        await sync_ask_state(session)
        if session.ask_in_flight:
            raise HTTPException(
                status_code=409,
                detail="an answer is being generated; wait for it or cancel the ask",
            )
        session.messages.append(ConsoleMessage(role=ConsoleMessageRole.USER, content=body.content))
        rolled = await enter_clarifying(session)
        if rolled:
            await emit(
                session,
                agent_event(
                    "orchestrator",
                    "clarify_round_started",
                    f"Clarify round {session.clarify_round}",
                    round=session.clarify_round,
                    questions=len(session.clarify_questions),
                ),
            )
        await touch(session)
        return session


@router.post("/sessions/{id}/clarify", response_model=ConsoleSession)
async def submit_clarify_answers(id: str, body: ClarifyRequest) -> ConsoleSession:
    await require_session(id)
    async with _lock_for(id):
        session = await require_session(id)
        if session.status is not ConsoleSessionStatus.CLARIFYING:
            raise HTTPException(
                status_code=409,
                detail=f"session is {session.status.value}, not awaiting clarification",
            )
        try:
            apply_clarify_answers(session, body.answers)
        except StaleClarifyRoundError as exc:
            # 409, not 400: the request was well formed and was correct when the client
            # rendered it. The question set moved on — refetch and answer the new one.
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        await touch(session)
        return session


@router.post("/sessions/{id}/confirm", response_model=ConsoleSession)
async def confirm_console_session(id: str) -> ConsoleSession:
    await require_session(id)
    async with _lock_for(id):
        session = await require_session(id)
        if session.status is not ConsoleSessionStatus.READY:
            raise HTTPException(
                status_code=409,
                detail=f"session is {session.status.value}; confirm requires ready",
            )
        session.confirmed = True
        await touch(session)
        return session


@router.post("/sessions/{id}/start", response_model=ConsoleSession, status_code=202)
async def start_console_run(id: str, body: StartRunRequest | None = None) -> ConsoleSession:
    """Queue the run and return 202 — accepted, not performed. Planning progress arrives
    on the event stream; the run becomes ``running`` when it wins the single slot.

    Both modes are asynchronous since M10: plan mode used to finish inside this handler,
    and now queues a real ScrumMaster/TechLead run whose result arrives on the stream.
    """
    await require_session(id)
    async with _lock_for(id):
        session = await require_session(id)
        if session.status is not ConsoleSessionStatus.READY:
            raise HTTPException(
                status_code=409,
                detail=f"session is {session.status.value}; start requires ready",
            )
        if not session.confirmed:
            raise HTTPException(status_code=409, detail="session must be confirmed before start")

        if session.mode is ConsoleMode.PLAN:
            # Plan mode never ships: no from-prompt run, no Jira, no git writes (ADR 0012).
            submit_plan_run(
                session,
                depth=resolve_depth(body.depth if body else None),
                prompt=build_run_prompt(session),
            )
            await touch(session)
            return session

        # Lazy import: app.py imports this router at module load.
        from sprint_crew.api.app import start_from_prompt_run

        try:
            run_id = await start_from_prompt_run(
                prompt=build_run_prompt(session),
                repo_url=session.repo_url,
                console_session_id=session.session_id,
            )
        except Exception as exc:
            session.status = ConsoleSessionStatus.FAILED
            session.error = str(exc)
            await touch(session)
            raise
        session.sprint_ref = SprintRunRef(backlog_run_id=run_id)
        # position 0 (or None) means nothing is ahead, so report running rather than making a
        # single run flicker queued→running for one poll. queue_position is only meaningful
        # while queued, so a falsy position is reported as null rather than 0.
        position = run_registry().position(run_id)
        session.status = ConsoleSessionStatus.QUEUED if position else ConsoleSessionStatus.RUNNING
        session.queue_position = position or None
        await touch(session)
        return session


@router.post("/sessions/{id}/cancel", response_model=ConsoleSession)
async def cancel_console_session(id: str) -> ConsoleSession:
    """Stop the session, and the run behind it if one is live. Always 200; ``status`` plus
    ``cancel_requested_at`` say which of two shapes happened.

    Nothing started, or still queued: terminal ``cancelled`` immediately. Executing: Stop is
    *accepted* — status stays ``running`` and a ``cancel_requested`` event goes out, flipping
    to ``cancelled`` once the run unwinds. That cannot be instant; the run stops at its next
    checkpoint (ADR 0014).
    """
    await require_session(id)
    async with _lock_for(id):
        session = await require_session(id)
        if session.status in _TERMINAL_STATUSES:
            raise HTTPException(status_code=409, detail=f"session already {session.status.value}")

        run_id = session.sprint_ref.backlog_run_id if session.sprint_ref else None
        registry = run_registry()
        await sync_ask_state(session)
        if session.active_ask_id is not None:
            # Ending the session ends the question asked inside it: otherwise the Explainer
            # keeps the lane and appends an answer to a session nobody is watching.
            registry.cancel(session.active_ask_id, reason="session cancelled")
        # A plan run is the session's run in plan mode, so it answers Stop the same way a
        # code run does. Liveness is read *before* cancelling: cancel dequeues a queued
        # entry, and asking afterwards would report it as executing.
        live_id = next(
            (rid for rid in (run_id, session.plan_run_id) if rid and registry.get(rid) is not None),
            None,
        )
        live = live_id is not None
        was_queued = live and registry.position(live_id) is not None
        if live:
            registry.cancel(live_id, reason="cancelled by user")
        # A hard cancel interrupts the parked node's own cleanup, so close the review here
        # too or a stopped session keeps advertising one nobody waits on. Idempotent
        # through the store's pending-only guard.
        if (review := await pending_review(session.session_id)) is not None:
            await close_review(
                session.session_id,
                review.sprint_session_id,
                review.attempt,
                status="expired",
                decided_at=_utc_now_iso(),
            )

        if live and not was_queued:
            session.cancel_requested_at = _utc_now_iso()
            await emit(
                session,
                agent_event(
                    "orchestrator",
                    "cancel_requested",
                    "Stopping run at the next checkpoint",
                    level="warning",
                    run_id=live_id,
                ),
            )
            await touch(session)
            return session

        session.status = ConsoleSessionStatus.CANCELLED
        session.queue_position = None
        if live:
            # A queued run's body never executes, so nothing else will update its row.
            # Plan mode has no backlog row at all, hence the guard rather than the flag.
            session.cancel_requested_at = _utc_now_iso()
            if run_id is not None:
                await asyncio.to_thread(cancel_backlog_run, run_id)
        await emit(
            session,
            agent_event("orchestrator", "cancelled", "Session cancelled", level="warning"),
        )
        await touch(session)
        await arun_console_reaper()
        return session
