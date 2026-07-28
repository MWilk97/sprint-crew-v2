"""Router, per-session locking, and the persistence seam. See AGENTS.md §4.2.

Depends on no other module in this package, which is what keeps the import graph acyclic.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException

from sprint_crew.api.auth import require_token
from sprint_crew.orchestrator.console_store import (
    console_store,
    enforce_workspace_lru,
    purge_console_session,
    reap_console_sessions,
)
from sprint_crew.orchestrator.diff_store import diff_store
from sprint_crew.orchestrator.event_bus import event_bus
from sprint_crew.orchestrator.event_log import event_log
from sprint_crew.orchestrator.plan_store import plan_store
from sprint_crew.orchestrator.review_gate import reset_review_gate
from sprint_crew.schemas.console import ConsoleSession, ConsoleSessionStatus
from sprint_crew.schemas.diff import (
    DiffReviewState,
    DiffSnapshotRef,
    FileDecision,
    FileDiff,
    WorkspaceDiffSnapshot,
)
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
_STARTED_STATUSES = frozenset(
    {
        ConsoleSessionStatus.QUEUED,
        ConsoleSessionStatus.RUNNING,
        # Parked on a diff review is still a started run — the way to speak to it is the
        # decisions endpoint, not another message.
        ConsoleSessionStatus.AWAITING_REVIEW,
    }
)

# One asyncio.Lock per session id, held across each handler's read-modify-write; a
# threading.Lock does not protect across await points, so clarify + start could interleave.
# Only ever keyed by an id confirmed to exist (require_session), or 404s would mint entries
# nothing reclaims. Evicted when a session is reaped.
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
    diff_store().clear()
    plan_store().clear()
    event_bus().clear()
    reset_review_gate()
    _locks.clear()


def run_console_reaper() -> list[str]:
    """Delete stale terminal sessions and evict their locks. Returns reaped ids.

    The workspace LRU runs on the same sweep: TTL alone no longer bounds disk now that
    every session is a clone (roadmap M8).
    """
    reaped = reap_console_sessions()
    for session_id in reaped:
        _locks.pop(session_id, None)
    enforce_workspace_lru()
    return reaped


async def arun_console_reaper() -> list[str]:
    return await asyncio.to_thread(run_console_reaper)


def _utc_now_iso() -> str:
    return datetime.now(tz=UTC).isoformat()


# --- the persistence seam ----------------------------------------------------------
# Every store and event-log access a handler makes goes through one of these, so "blocking
# SQLite never runs on the event loop" holds by construction (AGENTS.md §4.2).


async def load_session(session_id: str) -> ConsoleSession | None:
    return await asyncio.to_thread(console_store().load, session_id)


async def list_sessions_page(*, limit: int, offset: int) -> tuple[list[ConsoleSession], int]:
    return await asyncio.to_thread(console_store().list_page, limit=limit, offset=offset)


async def purge_session(session: ConsoleSession) -> None:
    await asyncio.to_thread(purge_console_session, session)


def drop_lock(session_id: str) -> None:
    """Evict a deleted session's lock so the table does not grow by one per delete."""
    _locks.pop(session_id, None)


async def require_session(session_id: str) -> ConsoleSession:
    """Load a session or 404. Call *before* taking the lock as well as inside it."""
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


async def latest_diff(session_id: str) -> WorkspaceDiffSnapshot | None:
    return await asyncio.to_thread(diff_store().latest, session_id)


async def latest_diff_key(session_id: str) -> tuple[str, int] | None:
    return await asyncio.to_thread(diff_store().latest_key, session_id)


async def diff_snapshot(
    session_id: str, sprint_session_id: str, attempt: int
) -> WorkspaceDiffSnapshot | None:
    return await asyncio.to_thread(diff_store().get, session_id, sprint_session_id, attempt)


async def diff_refs(session_id: str) -> list[DiffSnapshotRef]:
    return await asyncio.to_thread(diff_store().list_refs, session_id)


async def diff_file(
    session_id: str, sprint_session_id: str, attempt: int, path: str
) -> FileDiff | None:
    return await asyncio.to_thread(
        diff_store().get_file, session_id, sprint_session_id, attempt, path
    )


async def pending_review(session_id: str) -> DiffReviewState | None:
    return await asyncio.to_thread(diff_store().pending_review, session_id)


async def review_state(
    session_id: str, sprint_session_id: str, attempt: int
) -> DiffReviewState | None:
    return await asyncio.to_thread(diff_store().get_review, session_id, sprint_session_id, attempt)


async def record_review_decisions(
    session_id: str, sprint_session_id: str, attempt: int, decisions: list[FileDecision]
) -> None:
    await asyncio.to_thread(
        diff_store().record_decisions, session_id, sprint_session_id, attempt, decisions
    )


async def close_review(
    session_id: str, sprint_session_id: str, attempt: int, *, status: str, decided_at: str
) -> None:
    await asyncio.to_thread(
        diff_store().close_review,
        session_id,
        sprint_session_id,
        attempt,
        status=status,
        decided_at=decided_at,
    )


async def emit(session: ConsoleSession, event: AgentEvent) -> None:
    await emit_for(session.session_id, event)


async def emit_for(session_id: str, event: AgentEvent) -> None:
    """Append a console-level event to the timeline so both SSE and polling see it.

    Used for events the API layer owns — cancel, review decisions, workspace preparation.
    Run-level events come from the context emitter inside the run instead
    (orchestrator/emitter.py). Takes an id rather than a session because the workspace prep
    task publishes without holding one.

    TODO(bus): this breaks the ``EventBus`` invariant. ``append_many`` appends *and*
    publishes, so wrapping the whole thing in ``to_thread`` runs ``publish()`` on a worker
    thread — and ``asyncio.Queue`` is loop-bound, so a live SSE subscriber can miss the
    wakeup. Present since M5 (the cancel events). The events table is unaffected, so a
    reconnect always replays what was missed. Fix: give ``append_many`` a ``publish=False``
    flag, keep the SQLite write in the thread, and publish the returned seq-stamped events
    here on the loop.
    """
    await asyncio.to_thread(event_log().append_many, session_id, None, [event])
