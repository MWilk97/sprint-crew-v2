"""Shared sqlite key→JSON-payload persistence for orchestrator stores."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import ClassVar, cast

from pydantic import BaseModel


class SqliteJsonStore:
    """One-table upsert/load store; subclasses define the table layout.

    ``create_sql`` must create the table with ``key_column`` as PRIMARY KEY and
    a ``payload`` TEXT column (plus any extra columns the subclass writes).
    """

    table: ClassVar[str]
    key_column: ClassVar[str]
    create_sql: ClassVar[str]

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        # WAL + a busy timeout let SSE readers coexist with a writing run instead of
        # hitting "database is locked"; check_same_thread=False because connections are
        # opened per-operation and may be created off the event loop's thread.
        conn = sqlite3.connect(self.db_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.execute(self.create_sql)

    def _save_row(self, columns: dict[str, str]) -> None:
        names = list(columns)
        placeholders = ", ".join("?" for _ in names)
        updates = ", ".join(f"{name}=excluded.{name}" for name in names if name != self.key_column)
        with self._connect() as conn:
            conn.execute(
                f"INSERT INTO {self.table} ({', '.join(names)}) VALUES ({placeholders}) "
                f"ON CONFLICT({self.key_column}) DO UPDATE SET {updates}",
                tuple(columns[name] for name in names),
            )

    def _load_payload(self, key: str) -> str | None:
        with self._connect() as conn:
            row = conn.execute(
                f"SELECT payload FROM {self.table} WHERE {self.key_column} = ?",
                (key,),
            ).fetchone()
        if row is None:
            return None
        return str(row["payload"])

    def _delete(self, key: str) -> bool:
        with self._connect() as conn:
            cur = conn.execute(
                f"DELETE FROM {self.table} WHERE {self.key_column} = ?",
                (key,),
            )
        return cur.rowcount > 0

    def _list_payloads(self) -> list[str]:
        with self._connect() as conn:
            rows = conn.execute(f"SELECT payload FROM {self.table}").fetchall()
        return [str(row["payload"]) for row in rows]

    def _clear_all(self) -> None:
        with self._connect() as conn:
            conn.execute(f"DELETE FROM {self.table}")


class TypedJsonStore[T: BaseModel](SqliteJsonStore):
    """A ``SqliteJsonStore`` whose payload is one Pydantic model.

    The session, backlog-run and console-session stores each hand-rolled the identical
    save/load/list_all trio — nine lines apiece differing only in the model class. The
    key column is named after the model's own id field in all three, so the base can
    derive the key rather than have each subclass restate it.
    """

    model: ClassVar[type[BaseModel]]
    #: Whether the table carries an ``updated_at`` column mirrored from the model.
    tracks_updated_at: ClassVar[bool] = False

    def save(self, item: T) -> None:
        columns = {
            self.key_column: str(getattr(item, self.key_column)),
            "payload": json.dumps(item.model_dump(mode="json")),
        }
        if self.tracks_updated_at:
            columns["updated_at"] = str(getattr(item, "updated_at"))
        self._save_row(columns)

    def load(self, key: str) -> T | None:
        payload = self._load_payload(key)
        if payload is None:
            return None
        return cast(T, self.model.model_validate(json.loads(payload)))

    def list_all(self) -> list[T]:
        return [cast(T, self.model.model_validate(json.loads(p))) for p in self._list_payloads()]
