"""Durable persistence for /v1/console/* sessions (roadmap M1).

Console sessions were process-local memory; this store makes them survive an API
restart. Same sqlite file as the other orchestrator stores (``settings.session_db``).
The reaper deletes terminal sessions older than the TTL along with the workspaces of
their sprint runs — a console session owns no workspace directly, it references sprint
session ids whose clones live under ``settings.workspace_base``.
"""

from __future__ import annotations

import json
import logging
import shutil
from datetime import UTC, datetime

from sprint_crew.config import get_settings
from sprint_crew.orchestrator.store import SqliteJsonStore
from sprint_crew.schemas.console import ConsoleSession, ConsoleSessionStatus

logger = logging.getLogger(__name__)

_TERMINAL_STATUSES = frozenset(
    {
        ConsoleSessionStatus.COMPLETED,
        ConsoleSessionStatus.FAILED,
        ConsoleSessionStatus.CANCELLED,
    }
)


class ConsoleSessionStore(SqliteJsonStore):
    table = "console_sessions"
    key_column = "session_id"
    create_sql = """
        CREATE TABLE IF NOT EXISTS console_sessions (
            session_id TEXT PRIMARY KEY,
            payload TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
    """

    def save(self, session: ConsoleSession) -> None:
        self._save_row(
            {
                "session_id": session.session_id,
                "payload": json.dumps(session.model_dump(mode="json")),
                "updated_at": session.updated_at,
            }
        )

    def load(self, session_id: str) -> ConsoleSession | None:
        payload = self._load_payload(session_id)
        if payload is None:
            return None
        return ConsoleSession.model_validate(json.loads(payload))

    def delete(self, session_id: str) -> bool:
        return self._delete(session_id)

    def list_all(self) -> list[ConsoleSession]:
        return [ConsoleSession.model_validate(json.loads(p)) for p in self._list_payloads()]

    def clear(self) -> None:
        self._clear_all()


def console_store() -> ConsoleSessionStore:
    return ConsoleSessionStore(get_settings().session_db)


def _parse_updated_at(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def _delete_session_workspaces(session: ConsoleSession) -> None:
    base = get_settings().workspace_base
    if session.sprint_ref is None:
        return
    for sprint_session_id in session.sprint_ref.sprint_session_ids:
        dest = base / sprint_session_id
        if not dest.exists():
            continue
        try:
            shutil.rmtree(dest)
        except OSError:
            logger.exception("reaper: failed to delete workspace %s", dest)


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
        store.delete(session.session_id)
        reaped.append(session.session_id)
    if reaped:
        logger.info("reaper: deleted %d stale console session(s)", len(reaped))
    return reaped
