"""Append-only event backbone for console-session timelines (roadmap M2).

One monotonic ``seq`` cursor per console session, spanning every sprint session that
console run spawns. This replaces whole-blob rewrites of ``SprintSession.events`` for
the purpose of serving a timeline: events still flow into ``SprintState["events"]`` for
the graph's own use, and ``_persist_progress`` additionally appends the per-node delta
here with the console session id attached.

Not a ``SqliteJsonStore`` subclass: that base is one-row-per-key upsert, this table is
append-only and multi-row. It shares the same SQLite file and WAL/busy-timeout settings
so SSE readers (M3) can coexist with a writing run.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from sprint_crew.config import get_settings
from sprint_crew.orchestrator.event_bus import event_bus
from sprint_crew.schemas.session import AgentEvent

_CREATE_SQL = """
    CREATE TABLE IF NOT EXISTS events (
        console_session_id TEXT NOT NULL,
        seq INTEGER NOT NULL,
        sprint_session_id TEXT,
        timestamp TEXT NOT NULL,
        agent TEXT NOT NULL,
        event_type TEXT NOT NULL,
        phase TEXT,
        level TEXT NOT NULL,
        summary TEXT NOT NULL,
        detail_json TEXT,
        PRIMARY KEY (console_session_id, seq)
    )
"""
_INDEX_SQL = "CREATE INDEX IF NOT EXISTS idx_events_console ON events(console_session_id)"


class EventLog:
    """Append-only per-console-session event store with a monotonic cursor."""

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.execute(_CREATE_SQL)
            conn.execute(_INDEX_SQL)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    def append_many(
        self,
        console_session_id: str,
        sprint_session_id: str | None,
        events: list[AgentEvent],
    ) -> list[AgentEvent]:
        """Append events under a fresh contiguous seq range; return them seq-stamped.

        ``BEGIN IMMEDIATE`` takes the write lock before reading ``MAX(seq)``, so two
        concurrent appenders serialise rather than racing to the same seq. Runs are
        single-writer per console session in practice (one backlog run at a time,
        serialised on the GPU), so contention is rare; the lock is correctness, not
        throughput.
        """
        if not events:
            return []
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT COALESCE(MAX(seq), 0) AS m FROM events WHERE console_session_id = ?",
                (console_session_id,),
            ).fetchone()
            base = int(row["m"])
            stamped: list[AgentEvent] = []
            for offset, event in enumerate(events, start=1):
                seq = base + offset
                stamped.append(event.model_copy(update={"seq": seq}))
                conn.execute(
                    "INSERT INTO events (console_session_id, seq, sprint_session_id, "
                    "timestamp, agent, event_type, phase, level, summary, detail_json) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        console_session_id,
                        seq,
                        sprint_session_id,
                        event.timestamp,
                        event.agent,
                        event.event_type,
                        event.phase,
                        event.level,
                        event.summary,
                        json.dumps(event.detail) if event.detail is not None else None,
                    ),
                )
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()
        # Append-then-publish (M3): the table is committed above, so it is the source of
        # truth; the bus only notifies live SSE subscribers. A no-op when none are attached.
        bus = event_bus()
        for event in stamped:
            bus.publish(console_session_id, event)
        return stamped

    def read(self, console_session_id: str, since: int = 0, limit: int = 500) -> list[AgentEvent]:
        """Events with ``seq > since`` in seq order, capped at ``limit``."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM events WHERE console_session_id = ? AND seq > ? "
                "ORDER BY seq ASC LIMIT ?",
                (console_session_id, since, limit),
            ).fetchall()
        return [_row_to_event(row) for row in rows]

    def max_seq(self, console_session_id: str) -> int:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT COALESCE(MAX(seq), 0) AS m FROM events WHERE console_session_id = ?",
                (console_session_id,),
            ).fetchone()
        return int(row["m"])

    def has_events(self, console_session_id: str) -> bool:
        return self.max_seq(console_session_id) > 0

    def delete_for_session(self, console_session_id: str) -> None:
        """Drop one session's timeline. Only the delete route calls this — the reaper
        deliberately leaves events alone, as it always has."""
        with self._connect() as conn:
            conn.execute(
                "DELETE FROM events WHERE console_session_id = ?",
                (console_session_id,),
            )

    def clear(self) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM events")


def _row_to_event(row: sqlite3.Row) -> AgentEvent:
    detail = json.loads(row["detail_json"]) if row["detail_json"] is not None else None
    return AgentEvent(
        timestamp=row["timestamp"],
        agent=row["agent"],
        event_type=row["event_type"],
        summary=row["summary"],
        detail=detail,
        seq=int(row["seq"]),
        phase=row["phase"],
        level=row["level"],
    )


def event_log() -> EventLog:
    return EventLog(get_settings().session_db)
