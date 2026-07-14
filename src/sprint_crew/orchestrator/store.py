"""Shared sqlite key→JSON-payload persistence for orchestrator stores."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import ClassVar


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
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
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
