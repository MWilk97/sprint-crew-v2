"""Persistence for console attachments (roadmap M11, ADR 0019).

Metadata in SQLite, bytes on disk. A screenshot is megabytes and belongs in neither a
payload column nor the events table, so this follows ``DiffStore`` rather than
``SqliteJsonStore``: a hand-rolled table sharing the same file and its WAL settings.

Blobs live under ``CONSOLE_ATTACHMENT_BASE``, **not** under ``workspace_base``. That
directory is the session's git checkout, and writing untracked binaries into it would
show up in ``git status --porcelain`` — which ``plan_coverage.collect_changed_paths``
reads — and in the indexer's file walk. An upload must not be able to look like a change
the agent made.

Blobs are content-addressed, so pasting one screenshot twice costs one blob and returns
one attachment id.
"""

from __future__ import annotations

import hashlib
import logging
import shutil
import sqlite3
from contextlib import closing
from pathlib import Path

from sprint_crew.config import get_settings
from sprint_crew.orchestrator.store import _cached_store
from sprint_crew.schemas.attachment import Attachment, AttachmentKind

logger = logging.getLogger(__name__)

_CREATE_ATTACHMENTS_SQL = """
    CREATE TABLE IF NOT EXISTS attachments (
        attachment_id TEXT PRIMARY KEY,
        console_session_id TEXT NOT NULL,
        filename TEXT NOT NULL,
        media_type TEXT NOT NULL,
        kind TEXT NOT NULL,
        size_bytes INTEGER NOT NULL DEFAULT 0,
        sha256 TEXT NOT NULL,
        created_at TEXT NOT NULL
    )
"""
_INDEX_SQL = (
    "CREATE INDEX IF NOT EXISTS idx_attachments_console "
    "ON attachments(console_session_id, created_at)",
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_attachments_content "
    "ON attachments(console_session_id, sha256)",
)

_COLUMNS = (
    "attachment_id, console_session_id, filename, media_type, kind, size_bytes, sha256, created_at"
)


def content_hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def attachment_base() -> Path:
    """Root for every session's blobs. Read per call so a test can repoint it."""
    return get_settings().console_attachment_base


def blob_path(session_id: str, sha256: str) -> Path:
    return attachment_base() / session_id / sha256


class AttachmentStore:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as conn, conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute(_CREATE_ATTACHMENTS_SQL)
            for statement in _INDEX_SQL:
                conn.execute(statement)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    def save(self, attachment: Attachment, data: bytes) -> Attachment:
        """Write the blob and its row, or return the existing one for identical content.

        The blob is written before the row: a file with no row is unreferenced garbage the
        reaper collects anyway, whereas a row with no file is a 404 on something the API
        just said existed.
        """
        existing = self.find_by_hash(attachment.session_id, attachment.sha256)
        if existing is not None:
            return existing
        dest = blob_path(attachment.session_id, attachment.sha256)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
        with closing(self._connect()) as conn, conn:
            conn.execute(
                f"INSERT INTO attachments ({_COLUMNS}) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    attachment.attachment_id,
                    attachment.session_id,
                    attachment.filename,
                    attachment.media_type,
                    attachment.kind.value,
                    attachment.size_bytes,
                    attachment.sha256,
                    attachment.created_at,
                ),
            )
        return attachment

    def get(self, session_id: str, attachment_id: str) -> Attachment | None:
        """Scoped to the session in the URL, so one session can never read another's."""
        with closing(self._connect()) as conn, conn:
            row = conn.execute(
                f"SELECT {_COLUMNS} FROM attachments "
                "WHERE console_session_id = ? AND attachment_id = ?",
                (session_id, attachment_id),
            ).fetchone()
        return _from_row(row) if row is not None else None

    def find_by_hash(self, session_id: str, sha256: str) -> Attachment | None:
        with closing(self._connect()) as conn, conn:
            row = conn.execute(
                f"SELECT {_COLUMNS} FROM attachments WHERE console_session_id = ? AND sha256 = ?",
                (session_id, sha256),
            ).fetchone()
        return _from_row(row) if row is not None else None

    def list_for_session(self, session_id: str) -> list[Attachment]:
        with closing(self._connect()) as conn, conn:
            rows = conn.execute(
                f"SELECT {_COLUMNS} FROM attachments "
                "WHERE console_session_id = ? ORDER BY created_at ASC, attachment_id ASC",
                (session_id,),
            ).fetchall()
        return [_from_row(row) for row in rows]

    def get_many(self, session_id: str, attachment_ids: list[str]) -> list[Attachment]:
        """Resolve ids in the order the caller gave them, skipping any that do not exist.

        Order matters: these become prompt content parts, and a user who attached a
        before and an after screenshot means that order.
        """
        by_id = {a.attachment_id: a for a in self.list_for_session(session_id)}
        return [by_id[aid] for aid in attachment_ids if aid in by_id]

    def count_for_session(self, session_id: str) -> int:
        with closing(self._connect()) as conn, conn:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM attachments WHERE console_session_id = ?",
                (session_id,),
            ).fetchone()
        return int(row["n"])

    def read_blob(self, attachment: Attachment) -> bytes | None:
        path = blob_path(attachment.session_id, attachment.sha256)
        if not path.is_file():
            return None
        return path.read_bytes()

    def delete_for_console_session(self, session_id: str) -> None:
        with closing(self._connect()) as conn, conn:
            conn.execute("DELETE FROM attachments WHERE console_session_id = ?", (session_id,))
        _delete_dir(attachment_base() / session_id)

    def clear(self) -> None:
        with closing(self._connect()) as conn, conn:
            conn.execute("DELETE FROM attachments")


def _delete_dir(dest: Path) -> None:
    if not dest.exists():
        return
    try:
        shutil.rmtree(dest)
    except OSError:
        logger.exception("failed to delete attachment directory %s", dest)


def _from_row(row: sqlite3.Row) -> Attachment:
    return Attachment(
        attachment_id=row["attachment_id"],
        session_id=row["console_session_id"],
        filename=row["filename"],
        media_type=row["media_type"],
        kind=AttachmentKind(row["kind"]),
        size_bytes=int(row["size_bytes"]),
        sha256=row["sha256"],
        created_at=row["created_at"],
    )


def attachment_store() -> AttachmentStore:
    # Cached like the other stores: constructing one runs DDL, and the factory is called
    # from the persistence seam outside the to_thread that wraps the query.
    return _cached_store(AttachmentStore, get_settings().session_db)
