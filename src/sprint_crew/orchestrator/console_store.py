"""Durable persistence for /v1/console/* sessions (roadmap M1).

Console sessions were process-local memory; this store makes them survive an API
restart. Same sqlite file as the other orchestrator stores (``settings.session_db``).

Reclamation lives here too. Since M8 a session owns a clone of its own as well as
referencing its sprint runs' — both under ``settings.workspace_base``, and both go when
the session does. Two policies: the TTL reaper drops terminal sessions older than
``CONSOLE_SESSION_TTL_DAYS``, and the LRU evicts the coldest clones past
``CONSOLE_MAX_WORKSPACES`` without deleting the sessions themselves.
"""

from __future__ import annotations

import json
import logging
import shutil
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

from sprint_crew.config import get_settings
from sprint_crew.orchestrator.attachment_store import attachment_store
from sprint_crew.orchestrator.diff_store import diff_store
from sprint_crew.orchestrator.event_log import event_log
from sprint_crew.orchestrator.plan_store import plan_store
from sprint_crew.orchestrator.store import TypedJsonStore, _cached_store
from sprint_crew.schemas.console import (
    ConsoleSession,
    ConsoleSessionStatus,
    IndexStatus,
    WorkspaceStatus,
)

logger = logging.getLogger(__name__)


def _utc_now_iso() -> str:
    return datetime.now(tz=UTC).isoformat()


_TERMINAL_STATUSES = frozenset(
    {
        ConsoleSessionStatus.COMPLETED,
        ConsoleSessionStatus.FAILED,
        ConsoleSessionStatus.CANCELLED,
    }
)


class ConsoleSessionStore(TypedJsonStore[ConsoleSession]):
    model = ConsoleSession
    table = "console_sessions"
    key_column = "session_id"
    create_sql = """
        CREATE TABLE IF NOT EXISTS console_sessions (
            session_id TEXT PRIMARY KEY,
            payload TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
    """

    def save(self, item: ConsoleSession) -> None:
        # Stamped by the store, like SessionStore: every write is a state change worth
        # dating, and the reaper sorts on this column. Callers used to be responsible for
        # it, so one that forgot wrote a stale sort key.
        item.updated_at = _utc_now_iso()
        super().save(item)

    def _extra_columns(self, item: ConsoleSession) -> dict[str, str]:
        return {"updated_at": item.updated_at}

    def delete(self, session_id: str) -> bool:
        return self._delete(session_id)

    def list_page(self, *, limit: int, offset: int) -> tuple[list[ConsoleSession], int]:
        """Newest-first page of sessions, plus the total. Ordered in SQL rather than in
        Python because history is the one read that grows without bound."""
        with closing(self._connect()) as conn, conn:
            total = int(conn.execute(f"SELECT COUNT(*) AS n FROM {self.table}").fetchone()["n"])
            rows = conn.execute(
                f"SELECT payload FROM {self.table} ORDER BY updated_at DESC LIMIT ? OFFSET ?",
                (limit, offset),
            ).fetchall()
        return [ConsoleSession.model_validate(json.loads(row["payload"])) for row in rows], total

    def clear(self) -> None:
        self._clear_all()


def console_store() -> ConsoleSessionStore:
    return _cached_store(ConsoleSessionStore, get_settings().session_db)


def _parse_updated_at(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def _delete_dir(dest: Path) -> None:
    if not dest.exists():
        return
    try:
        shutil.rmtree(dest)
    except OSError:
        logger.exception("failed to delete workspace %s", dest)


def _delete_session_workspaces(session: ConsoleSession) -> None:
    """Every clone this session is responsible for: its own, plus its runs'."""
    base = get_settings().workspace_base
    _delete_dir(base / session.session_id)
    if session.sprint_ref is None:
        return
    for sprint_session_id in session.sprint_ref.sprint_session_ids:
        _delete_dir(base / sprint_session_id)


def purge_console_session(session: ConsoleSession) -> None:
    """Delete a session and everything it owns.

    The reaper drops the row, its clones and its diffs but leaves the timeline; a
    user-triggered delete is a different intent — they asked for it gone — so the events
    go too.
    """
    _delete_session_workspaces(session)
    diff_store().delete_for_console_session(session.session_id)
    attachment_store().delete_for_console_session(session.session_id)
    event_log().delete_for_session(session.session_id)
    plan_store().delete(session.session_id)
    console_store().delete(session.session_id)


def reap_console_sessions(now: datetime | None = None) -> list[str]:
    """Delete terminal sessions older than the TTL, plus their sprint workspaces.

    Returns the ids that were reaped so callers can evict per-session locks.
    """
    settings = get_settings()
    now = now or datetime.now(tz=UTC)
    cutoff_seconds = settings.console_session_ttl_days * 86400.0
    store = console_store()
    reaped: list[str] = []
    for session in store.list_all():
        if session.status not in _TERMINAL_STATUSES:
            continue
        updated = _parse_updated_at(session.updated_at)
        if updated is None or (now - updated).total_seconds() < cutoff_seconds:
            continue
        _delete_session_workspaces(session)
        # Diff snapshots and attachment blobs are the pieces of session state large enough
        # to be worth reclaiming explicitly; the events table is left alone, as it always
        # has been.
        diff_store().delete_for_console_session(session.session_id)
        attachment_store().delete_for_console_session(session.session_id)
        plan_store().delete(session.session_id)
        store.delete(session.session_id)
        reaped.append(session.session_id)
    if reaped:
        logger.info("reaper: deleted %d stale console session(s)", len(reaped))
    return reaped


def enforce_workspace_lru() -> list[str]:
    """Keep at most ``CONSOLE_MAX_WORKSPACES`` clones, dropping the coldest terminal ones.

    Every session is a checkout now, so the TTL alone no longer bounds disk — a busy
    afternoon can create a dozen before any of them is old enough to reap. Only terminal
    sessions are candidates: a live one still needs its files.
    """
    settings = get_settings()
    cap = settings.console_max_workspaces
    if cap <= 0:
        return []
    store = console_store()
    held = sorted(
        (s for s in store.list_all() if s.workspace_status is WorkspaceStatus.READY),
        key=lambda s: s.updated_at,
    )
    evicted: list[str] = []
    for session in held[: max(0, len(held) - cap)]:
        if session.status not in _TERMINAL_STATUSES:
            continue
        _delete_session_workspaces(session)
        session.workspace_status = WorkspaceStatus.EVICTED
        session.workspace_root = None
        store.save(session)
        evicted.append(session.session_id)
    if evicted:
        logger.info("workspace LRU: evicted %d clone(s)", len(evicted))
    return evicted


def sweep_interrupted_workspace_prep() -> list[str]:
    """Fail rows left mid-preparation by a restart.

    Prep runs in an asyncio task, so a restart kills it without touching its row — the
    same lie ``sweep_interrupted_runs`` fixes for runs, and it would otherwise leave a
    session advertising "cloning" forever.
    """
    store = console_store()
    swept: list[str] = []
    for session in store.list_all():
        cloning = session.workspace_status is WorkspaceStatus.CLONING
        indexing = session.index_status is IndexStatus.INDEXING
        if not (cloning or indexing):
            continue
        if cloning:
            session.workspace_status = WorkspaceStatus.FAILED
            session.workspace_error = "interrupted by restart"
            session.index_status = IndexStatus.SKIPPED
        else:
            session.index_status = IndexStatus.FAILED
            session.index_error = "interrupted by restart"
        store.save(session)
        swept.append(session.session_id)
    if swept:
        logger.info("startup: failed %d interrupted workspace prep(s)", len(swept))
    return swept
