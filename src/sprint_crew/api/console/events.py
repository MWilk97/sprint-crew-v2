"""Timeline transport: the polling endpoint and the SSE stream.

The events table is the source of truth; the bus is a live view over it. Both endpoints
serve the same AgentEvent payload, so a client can switch between them freely.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

from fastapi import Request
from sse_starlette import EventSourceResponse, ServerSentEvent

from sprint_crew.api.console.state import (
    _TERMINAL_STATUSES,
    _lock_for,
    has_events,
    load_session,
    max_event_seq,
    read_events,
    require_session,
    router,
)
from sprint_crew.config import get_settings
from sprint_crew.orchestrator.backlog import get_backlog_run
from sprint_crew.orchestrator.event_bus import event_bus
from sprint_crew.orchestrator.event_log import EventLog, event_log
from sprint_crew.orchestrator.session import get_session
from sprint_crew.schemas.console import ConsoleEventsPage, ConsoleSession
from sprint_crew.schemas.session import AgentEvent, BacklogRunStatus


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


async def _backfill(session: ConsoleSession) -> None:
    """The one composite in this module — it reads sprint sessions as well as the log, so
    it gets an explicit hop off the loop rather than a helper in ``state``."""
    await asyncio.to_thread(_backfill_events, session, event_log())


@router.get("/sessions/{id}/events", response_model=ConsoleEventsPage)
async def get_console_events(id: str, since: int = 0, limit: int = 500) -> ConsoleEventsPage:
    """The console timeline, served by polling.

    ``seq`` is monotonic across every sprint session this console run spawned; poll
    again with ``since=next_seq`` to drain the next page. ``complete`` reports whether
    the session reached a terminal status, not whether this page is the last — a client
    keeps polling until it receives an empty page while ``complete`` is true.
    """
    limit = max(1, min(limit, 1000))
    await require_session(id)
    async with _lock_for(id):
        session = await require_session(id)
        if since <= 0 and not await has_events(id):
            await _backfill(session)
        events = await read_events(id, since=since, limit=limit)
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
    (``sync_sprint_progress``), so consult the backlog run directly rather than trusting the
    stored status alone. A reaped/missing session also counts as complete so the stream ends.
    """
    session = await load_session(session_id)
    if session is None or session.status in _TERMINAL_STATUSES:
        return True
    ref = session.sprint_ref
    if ref is not None and ref.backlog_run_id is not None:
        run = await asyncio.to_thread(get_backlog_run, ref.backlog_run_id)
        if run is not None and run.status in (
            BacklogRunStatus.COMPLETED,
            BacklogRunStatus.FAILED,
            BacklogRunStatus.CANCELLED,
        ):
            return True
    return False


@router.get("/sessions/{id}/stream")
async def stream_console_events(id: str, request: Request, since: int = 0) -> EventSourceResponse:
    """Live SSE timeline — same payload as the polling events endpoint, pushed.

    Resume: the browser sends ``Last-Event-ID`` automatically on reconnect (or pass
    ``?since=``); we replay from the events table up to the current tail, then stream new
    events off the in-process bus. Subscribing *before* the replay closes the handoff gap —
    events appended mid-replay land in the queue and are de-duplicated by ``seq``. A heartbeat
    comment every ``SSE_HEARTBEAT_S`` survives proxy idle-reap and long GPU silences; a
    terminal run ends with an explicit ``event: done`` so the client stops reconnecting.

    Auth: browser ``EventSource`` cannot set headers, so ``require_token`` also accepts
    ``?token=`` (see api/auth.py).
    """
    await require_session(id)

    last_event_id = request.headers.get("last-event-id")
    if last_event_id is not None:
        try:
            since = int(last_event_id)
        except ValueError:
            pass
    cursor = max(since, 0)

    if cursor <= 0 and not await has_events(id):
        async with _lock_for(id):
            fresh = await require_session(id)
            if not await has_events(id):
                await _backfill(fresh)

    heartbeat = get_settings().sse_heartbeat_s
    bus = event_bus()

    async def _events() -> AsyncIterator[ServerSentEvent]:
        nonlocal cursor
        # Subscribed inside the generator, not beside it: the `finally` that unsubscribes
        # only runs once the generator has started, so a client that disconnects before
        # the first iteration used to leave its subscription behind. Still before the
        # replay below, which is what closes the handoff gap (events appended mid-replay
        # land in the queue and are de-duplicated by seq).
        queue = bus.subscribe(id)
        try:
            while True:
                batch = await read_events(id, since=cursor, limit=500)
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
                    tail = await max_event_seq(id)
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
