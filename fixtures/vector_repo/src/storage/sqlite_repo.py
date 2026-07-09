"""SQLite persistence for tasks and outbound queue."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from messaging.models import DeliveryStatus, Message


class SqliteRepository:
    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self._db_path)

    def _init_schema(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS tasks (
                    id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    done INTEGER NOT NULL DEFAULT 0
                )
                """
            )
            # outbound_queue table added by story 1

    def list_tasks(self) -> list[dict[str, object]]:
        with self._connect() as conn:
            rows = conn.execute("SELECT id, title, done FROM tasks").fetchall()
        return [{"id": r[0], "title": r[1], "done": bool(r[2])} for r in rows]

    def create_task(self, task_id: str, title: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO tasks (id, title, done) VALUES (?, ?, 0)",
                (task_id, title),
            )

    def enqueue_message(self, message: Message) -> None:
        """Persist outbound message for ferry dispatch. Not implemented — story 1."""
        raise NotImplementedError("outbound queue not implemented")

    def dequeue_pending(self) -> list[Message]:
        """Return pending messages for ferry. Not implemented — story 1."""
        raise NotImplementedError("outbound queue not implemented")

    def update_message_status(self, message_id: str, status: DeliveryStatus) -> None:
        raise NotImplementedError("outbound queue not implemented")
