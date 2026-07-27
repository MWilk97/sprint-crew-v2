"""Router, per-session locking, and the single persistence seam for console handlers.

Everything here is shared by every other module in this package and depends on none of
them, which is what keeps the import graph acyclic.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException

from sprint_crew.api.auth import require_token
from sprint_crew.orchestrator.console_store import console_store, reap_console_sessions
from sprint_crew.orchestrator.event_bus import event_bus
from sprint_crew.orchestrator.event_log import event_log
from sprint_crew.schemas.console import ConsoleSession, ConsoleSessionStatus
from sprint_crew.schemas.session import AgentEvent

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

# A run has been handed to the registry — the prompt is fixed and further messages are
# meaningless until it ends. ``queued`` counts: mid-run steering is out of scope, and
# accepting a message that silently changes nothing is worse than a 409.
_STARTED_STATUSES = frozenset({ConsoleSessionStatus.QUEUED, ConsoleSessionStatus.RUNNING})

# One asyncio.Lock per session id, held across each handler's read-modify-write.
# threading.Lock did not protect across await points inside one event loop, so
# concurrent clarify + start could interleave. Entries are evicted when a session
# is reaped. Single-worker assumption: this dict is not shared across processes.
#
# Only ever keyed by an id that has been confirmed to exist — see require_session. Every
# handler used to lock first and look up second, so a request for any made-up id minted a
# permanent entry and nothing ever reclaimed it.
_locks: dict[str, asyncio.Lock] = {}


def _lock_for(session_id: str) -> asyncio.Lock:
    # Safe without its own guard: the event loop never preempts between these two
    # lines (no await), so two callers cannot create competing locks for one id.
    lock = _locks.get(session_id)
    if lock is None:
        lock = asyncio.Lock()
        _locks[session_id] = lock
    return lock


def reset_console_locks() -> None:
    """Drop the process-local locks without touching the store — i.e. simulate a restart."""
    _locks.clear()


def lock_count() -> int:
    """Live lock entries. Exposed so a test can assert the table does not grow unbounded."""
    return len(_locks)


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


async def arun_console_reaper() -> list[str]:
    return await asyncio.to_thread(run_console_reaper)


def _utc_now_iso() -> str:
    return datetime.now(tz=UTC).isoformat()


# --- the persistence seam ----------------------------------------------------------
#
# Every store and event-log access a handler makes goes through one of these. They are the
# only place `asyncio.to_thread` appears in this package, which is what makes "blocking
# SQLite never runs on the event loop" a property of the code rather than of reviewer
# vigilance — previously some handlers threaded these reads and others made the identical
# call inline.


async def load_session(session_id: str) -> ConsoleSession | None:
    return await asyncio.to_thread(console_store().load, session_id)


async def require_session(session_id: str) -> ConsoleSession:
    """Load a session or 404.

    Call this *before* taking the session lock as well as inside it: locking first is what
    let an unknown id allocate a lock entry that nothing ever reclaimed.
    """
    session = await load_session(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Console session not found")
    return session


async def touch(session: ConsoleSession) -> None:
    """Persist the session, stamping updated_at. The single save seam for every handler."""
    session.updated_at = _utc_now_iso()
    await asyncio.to_thread(console_store().save, session)


async def has_events(session_id: str) -> bool:
    return await asyncio.to_thread(event_log().has_events, session_id)


async def read_events(session_id: str, *, since: int, limit: int) -> list[AgentEvent]:
    return await asyncio.to_thread(event_log().read, session_id, since=since, limit=limit)


async def max_event_seq(session_id: str) -> int:
    return await asyncio.to_thread(event_log().max_seq, session_id)


async def emit(session: ConsoleSession, event: AgentEvent) -> None:
    """Append a console-level event to the timeline so both SSE and polling see it.

    Used for events the API layer owns — cancel, in particular. Run-level events come from
    the context emitter inside the run instead (orchestrator/emitter.py).
    """
    await asyncio.to_thread(event_log().append_many, session.session_id, None, [event])
