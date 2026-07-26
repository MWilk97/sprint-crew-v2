"""The console session lifecycle routes: create, read, message, clarify, confirm, start, cancel.

Every handler is a thin orchestration over state/clarify/run_bridge, so this file reads as
the API contract. The two timeline routes live in ``events.py`` with the transport they
belong to.
"""

from __future__ import annotations

import asyncio
from uuid import uuid4

from fastapi import HTTPException

from sprint_crew.api.console.clarify import apply_clarify_answers, enter_clarifying
from sprint_crew.api.console.run_bridge import (
    build_plan_result,
    build_run_prompt,
    cancel_backlog_run,
    sync_sprint_progress,
)
from sprint_crew.api.console.state import (
    _STARTED_STATUSES,
    _TERMINAL_STATUSES,
    _emit,
    _get_session_or_404,
    _lock_for,
    _touch,
    _utc_now_iso,
    router,
    run_console_reaper,
)
from sprint_crew.orchestrator.run_registry import run_registry
from sprint_crew.schemas.console import (
    ClarifyRequest,
    ConsoleMessage,
    ConsoleMessageRole,
    ConsoleMode,
    ConsoleSession,
    ConsoleSessionStatus,
    CreateConsoleSessionRequest,
    PostMessageRequest,
    SprintRunRef,
)
from sprint_crew.schemas.session import agent_event


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
            await enter_clarifying(session)
        _touch(session)
    return session


@router.get("/sessions/{id}", response_model=ConsoleSession)
async def get_console_session(id: str) -> ConsoleSession:
    async with _lock_for(id):
        session = _get_session_or_404(id)
        await sync_sprint_progress(session)
        if session.status in _TERMINAL_STATUSES:
            run_console_reaper()
        return session


@router.post("/sessions/{id}/messages", response_model=ConsoleSession)
async def post_console_message(id: str, body: PostMessageRequest) -> ConsoleSession:
    async with _lock_for(id):
        session = _get_session_or_404(id)
        if session.status in _STARTED_STATUSES or session.status in _TERMINAL_STATUSES:
            raise HTTPException(
                status_code=409,
                detail=f"session is {session.status.value}; no further messages accepted",
            )
        session.messages.append(ConsoleMessage(role=ConsoleMessageRole.USER, content=body.content))
        if session.status is ConsoleSessionStatus.COLLECTING:
            await enter_clarifying(session)
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


@router.post("/sessions/{id}/start", response_model=ConsoleSession, status_code=202)
async def start_console_run(id: str) -> ConsoleSession:
    """Queue the run and return (M5). Planning progress arrives on the event stream.

    202, not 200: the run has been accepted, not performed. It lands in ``queued`` and
    becomes ``running`` when it wins the single run slot.
    """
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
                console_session_id=session.session_id,
            )
        except Exception as exc:
            session.status = ConsoleSessionStatus.FAILED
            session.error = str(exc)
            _touch(session)
            raise
        session.sprint_ref = SprintRunRef(backlog_run_id=run_id)
        # position 0 (or None) means nothing is ahead, so report running rather than making a
        # single run flicker queued→running for one poll. queue_position is only meaningful
        # while queued, so a falsy position is reported as null rather than 0.
        position = run_registry().position(run_id)
        session.status = ConsoleSessionStatus.QUEUED if position else ConsoleSessionStatus.RUNNING
        session.queue_position = position or None
        _touch(session)
        return session


@router.post("/sessions/{id}/cancel", response_model=ConsoleSession)
async def cancel_console_session(id: str) -> ConsoleSession:
    """Stop the session, and the run behind it if one is live (M5).

    Three shapes, all answered 200 — ``status`` plus ``cancel_requested_at`` say which:

    - nothing started, or the run is still queued: terminal ``cancelled`` immediately;
    - the run is executing: Stop is *accepted*. ``cancel_requested_at`` is set, the status
      stays ``running``, and a ``cancel_requested`` event goes out so the UI can show
      "stopping…". It flips to ``cancelled`` once the run actually unwinds.

    A running cancel cannot be instant: the run stops at its next checkpoint, and a
    checkpoint is invisible while a subprocess is mid-call. Worst case is bounded by
    ``ACCEPTANCE_TEST_TIMEOUT_S`` / ``run_command``'s own timeout, not by ``CANCEL_GRACE_S``
    alone — the grace window only governs when the task is hard-cancelled.
    """
    async with _lock_for(id):
        session = _get_session_or_404(id)
        if session.status in _TERMINAL_STATUSES:
            raise HTTPException(status_code=409, detail=f"session already {session.status.value}")

        run_id = session.sprint_ref.backlog_run_id if session.sprint_ref else None
        registry = run_registry()
        live = run_id is not None and registry.get(run_id) is not None
        was_queued = live and registry.position(run_id) is not None
        if live:
            registry.cancel(run_id, reason="cancelled by user")

        if live and not was_queued:
            session.cancel_requested_at = _utc_now_iso()
            _emit(
                session,
                agent_event(
                    "orchestrator",
                    "cancel_requested",
                    "Stopping run at the next checkpoint",
                    level="warning",
                    run_id=run_id,
                ),
            )
            _touch(session)
            return session

        session.status = ConsoleSessionStatus.CANCELLED
        session.queue_position = None
        if live:
            # A queued run's body never executes, so nothing else will update its row.
            session.cancel_requested_at = _utc_now_iso()
            await asyncio.to_thread(cancel_backlog_run, run_id)
        _emit(
            session,
            agent_event("orchestrator", "cancelled", "Session cancelled", level="warning"),
        )
        _touch(session)
        run_console_reaper()
        return session
