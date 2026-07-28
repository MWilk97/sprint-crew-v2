"""Persistence for structured diff snapshots (roadmap M6).

Not a ``SqliteJsonStore`` subclass, for the same reason ``EventLog`` is not: that base is
one-row-per-key upsert of a single payload, and this is two related multi-row tables. Shares
the SQLite file and its WAL/busy-timeout settings so SSE readers coexist with a writing run.

Two tables rather than one blob per snapshot: rendering a file tree must not mean
deserialising every hunk of every file, and a large change is megabytes of hunks. The file
list comes from ``diff_files`` without touching ``hunks_json`` at all.

``console_session_id`` is denormalised onto both tables. It makes every read a
single guarded statement — a caller can only ever reach snapshots belonging to the session
in the URL — and makes the reaper one delete per table.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from pathlib import Path

from sprint_crew.config import get_settings
from sprint_crew.orchestrator.store import _cached_store
from sprint_crew.schemas.diff import (
    DiffFileSummary,
    DiffReviewState,
    DiffSnapshotRef,
    FileDecision,
    FileDiff,
    WorkspaceDiffSnapshot,
)

_CREATE_SNAPSHOTS_SQL = """
    CREATE TABLE IF NOT EXISTS diff_snapshots (
        sprint_session_id TEXT NOT NULL,
        attempt INTEGER NOT NULL,
        console_session_id TEXT,
        ticket_key TEXT NOT NULL DEFAULT '',
        git_sha TEXT NOT NULL DEFAULT '',
        captured_at TEXT NOT NULL,
        total_additions INTEGER NOT NULL DEFAULT 0,
        total_deletions INTEGER NOT NULL DEFAULT 0,
        truncated INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (sprint_session_id, attempt)
    )
"""
_CREATE_FILES_SQL = """
    CREATE TABLE IF NOT EXISTS diff_files (
        sprint_session_id TEXT NOT NULL,
        attempt INTEGER NOT NULL,
        path TEXT NOT NULL,
        console_session_id TEXT,
        old_path TEXT,
        action TEXT NOT NULL,
        additions INTEGER NOT NULL DEFAULT 0,
        deletions INTEGER NOT NULL DEFAULT 0,
        binary INTEGER NOT NULL DEFAULT 0,
        truncated INTEGER NOT NULL DEFAULT 0,
        header_json TEXT NOT NULL,
        hunks_json TEXT NOT NULL,
        PRIMARY KEY (sprint_session_id, attempt, path)
    )
"""
_CREATE_REVIEWS_SQL = """
    CREATE TABLE IF NOT EXISTS diff_reviews (
        sprint_session_id TEXT NOT NULL,
        attempt INTEGER NOT NULL,
        console_session_id TEXT,
        status TEXT NOT NULL,
        rejection_round INTEGER NOT NULL DEFAULT 0,
        requested_at TEXT NOT NULL,
        expires_at TEXT NOT NULL DEFAULT '',
        decided_at TEXT,
        PRIMARY KEY (sprint_session_id, attempt)
    )
"""
_CREATE_DECISIONS_SQL = """
    CREATE TABLE IF NOT EXISTS diff_decisions (
        sprint_session_id TEXT NOT NULL,
        attempt INTEGER NOT NULL,
        path TEXT NOT NULL,
        console_session_id TEXT,
        decision TEXT NOT NULL,
        reason TEXT,
        decided_at TEXT NOT NULL,
        PRIMARY KEY (sprint_session_id, attempt, path)
    )
"""
_INDEX_SQL = (
    "CREATE INDEX IF NOT EXISTS idx_diff_snapshots_console "
    "ON diff_snapshots(console_session_id, captured_at)",
    "CREATE INDEX IF NOT EXISTS idx_diff_files_console ON diff_files(console_session_id)",
    "CREATE INDEX IF NOT EXISTS idx_diff_reviews_console "
    "ON diff_reviews(console_session_id, status)",
    "CREATE INDEX IF NOT EXISTS idx_diff_decisions_console ON diff_decisions(console_session_id)",
)

_SUMMARY_COLUMNS = "path, old_path, action, additions, deletions, binary, truncated"

# Tie-broken on the key: two captures a microsecond apart must not swap places between
# reads, or a client's story picker reorders under it.
_LATEST_ORDER = "ORDER BY captured_at DESC, sprint_session_id DESC, attempt DESC LIMIT 1"


class DiffStore:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as conn, conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute(_CREATE_SNAPSHOTS_SQL)
            conn.execute(_CREATE_FILES_SQL)
            conn.execute(_CREATE_REVIEWS_SQL)
            conn.execute(_CREATE_DECISIONS_SQL)
            for statement in _INDEX_SQL:
                conn.execute(statement)

    def _connect(self) -> sqlite3.Connection:
        # journal_mode is set once in __init__, not here: it lives in the database header, so
        # re-issuing it per connection costs a statement on every read and buys nothing.
        # busy_timeout is per-connection and does have to be repeated.
        conn = sqlite3.connect(self.db_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    def save(self, snapshot: WorkspaceDiffSnapshot, files: list[FileDiff]) -> None:
        """Write a snapshot and its files, replacing any previous capture of that attempt.

        Replace rather than append: a re-review of the same attempt describes the same
        tree, and keeping both would leave the API picking between two truths.
        """
        key = (snapshot.sprint_session_id, snapshot.attempt)
        with closing(self._connect()) as conn, conn:
            conn.execute("DELETE FROM diff_files WHERE sprint_session_id = ? AND attempt = ?", key)
            # OR REPLACE rather than ON CONFLICT DO UPDATE: every non-key column is
            # overwritten, so the upsert form would just restate all nine column names twice.
            conn.execute(
                "INSERT OR REPLACE INTO diff_snapshots (sprint_session_id, attempt, "
                "console_session_id, ticket_key, git_sha, captured_at, total_additions, "
                "total_deletions, truncated) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    *key,
                    snapshot.console_session_id,
                    snapshot.ticket_key,
                    snapshot.git_sha,
                    snapshot.captured_at,
                    snapshot.total_additions,
                    snapshot.total_deletions,
                    int(snapshot.truncated),
                ),
            )
            conn.executemany(
                "INSERT INTO diff_files (sprint_session_id, attempt, path, console_session_id, "
                "old_path, action, additions, deletions, binary, truncated, header_json, "
                "hunks_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    (
                        *key,
                        f.path,
                        snapshot.console_session_id,
                        f.old_path,
                        f.action,
                        f.additions,
                        f.deletions,
                        int(f.binary),
                        int(f.truncated),
                        json.dumps(f.header_lines),
                        json.dumps([h.model_dump(mode="json") for h in f.hunks]),
                    )
                    for f in files
                ],
            )

    def latest(self, console_session_id: str) -> WorkspaceDiffSnapshot | None:
        """The most recently captured snapshot for a console session.

        Ordered by capture time, so for a multi-story backlog run this is the story
        currently under review rather than the first one.
        """
        with closing(self._connect()) as conn, conn:
            row = conn.execute(
                f"SELECT * FROM diff_snapshots WHERE console_session_id = ? {_LATEST_ORDER}",
                (console_session_id,),
            ).fetchone()
            return self._snapshot_from_row(conn, row) if row is not None else None

    def latest_key(self, console_session_id: str) -> tuple[str, int] | None:
        """Which snapshot ``latest`` would return, without reading its file rows.

        The per-file route needs the key and nothing else; resolving it through ``latest``
        meant loading up to ``DIFF_MAX_FILES`` summaries only to throw them away.
        """
        with closing(self._connect()) as conn, conn:
            row = conn.execute(
                "SELECT sprint_session_id, attempt FROM diff_snapshots "
                f"WHERE console_session_id = ? {_LATEST_ORDER}",
                (console_session_id,),
            ).fetchone()
        return (row["sprint_session_id"], int(row["attempt"])) if row is not None else None

    def get(
        self, console_session_id: str, sprint_session_id: str, attempt: int
    ) -> WorkspaceDiffSnapshot | None:
        with closing(self._connect()) as conn, conn:
            row = conn.execute(
                "SELECT * FROM diff_snapshots WHERE console_session_id = ? "
                "AND sprint_session_id = ? AND attempt = ?",
                (console_session_id, sprint_session_id, attempt),
            ).fetchone()
            return self._snapshot_from_row(conn, row) if row is not None else None

    def list_refs(self, console_session_id: str) -> list[DiffSnapshotRef]:
        with closing(self._connect()) as conn, conn:
            rows = conn.execute(
                "SELECT s.*, COUNT(f.path) AS files_changed FROM diff_snapshots s "
                "LEFT JOIN diff_files f ON f.sprint_session_id = s.sprint_session_id "
                "AND f.attempt = s.attempt WHERE s.console_session_id = ? "
                "GROUP BY s.sprint_session_id, s.attempt "
                "ORDER BY s.captured_at ASC, s.sprint_session_id ASC, s.attempt ASC",
                (console_session_id,),
            ).fetchall()
        return [
            DiffSnapshotRef(
                sprint_session_id=row["sprint_session_id"],
                attempt=int(row["attempt"]),
                ticket_key=row["ticket_key"],
                captured_at=row["captured_at"],
                files_changed=int(row["files_changed"]),
                total_additions=int(row["total_additions"]),
                total_deletions=int(row["total_deletions"]),
            )
            for row in rows
        ]

    def get_file(
        self, console_session_id: str, sprint_session_id: str, attempt: int, path: str
    ) -> FileDiff | None:
        with closing(self._connect()) as conn, conn:
            row = conn.execute(
                "SELECT * FROM diff_files WHERE console_session_id = ? AND "
                "sprint_session_id = ? AND attempt = ? AND path = ?",
                (console_session_id, sprint_session_id, attempt, path),
            ).fetchone()
        if row is None:
            return None
        return FileDiff(
            **_summary_fields(row),
            header_lines=json.loads(row["header_json"]),
            hunks=json.loads(row["hunks_json"]),
        )

    # --- human review (M7) ---------------------------------------------------------
    # Decisions live beside the snapshot rather than on it: a snapshot describes one
    # immutable capture of the tree, and the same capture can be re-read after the review
    # closed. Joining on read keeps the file rows write-once.

    def open_review(
        self,
        console_session_id: str,
        sprint_session_id: str,
        attempt: int,
        *,
        rejection_round: int,
        requested_at: str,
        expires_at: str,
    ) -> None:
        """Park a snapshot on a human decision. Replaces any previous review of that attempt.

        Replace rather than fail: a re-entered gate for the same attempt (a resumed graph,
        a retried capture) describes the same tree, and two open reviews for one snapshot
        would leave the release condition ambiguous.
        """
        with closing(self._connect()) as conn, conn:
            conn.execute(
                "INSERT OR REPLACE INTO diff_reviews (sprint_session_id, attempt, "
                "console_session_id, status, rejection_round, requested_at, expires_at, "
                "decided_at) VALUES (?, ?, ?, 'pending', ?, ?, ?, NULL)",
                (
                    sprint_session_id,
                    attempt,
                    console_session_id,
                    rejection_round,
                    requested_at,
                    expires_at,
                ),
            )

    def get_review(
        self, console_session_id: str, sprint_session_id: str, attempt: int
    ) -> DiffReviewState | None:
        with closing(self._connect()) as conn, conn:
            row = conn.execute(
                "SELECT * FROM diff_reviews WHERE console_session_id = ? "
                "AND sprint_session_id = ? AND attempt = ?",
                (console_session_id, sprint_session_id, attempt),
            ).fetchone()
            return self._review_from_row(conn, row) if row is not None else None

    def pending_review(self, console_session_id: str) -> DiffReviewState | None:
        """The review blocking this console session, if any.

        At most one can be open — a console session runs one story at a time and a story
        parks once per attempt — but the ordering makes the "newest wins" tie-break
        explicit rather than leaving it to SQLite's row order.
        """
        with closing(self._connect()) as conn, conn:
            row = conn.execute(
                "SELECT * FROM diff_reviews WHERE console_session_id = ? AND status = 'pending' "
                "ORDER BY requested_at DESC, sprint_session_id DESC, attempt DESC LIMIT 1",
                (console_session_id,),
            ).fetchone()
            return self._review_from_row(conn, row) if row is not None else None

    def record_decisions(
        self,
        console_session_id: str,
        sprint_session_id: str,
        attempt: int,
        decisions: list[FileDecision],
    ) -> None:
        """Upsert one verdict per path. Last write wins, so a user can change their mind."""
        with closing(self._connect()) as conn, conn:
            conn.executemany(
                "INSERT OR REPLACE INTO diff_decisions (sprint_session_id, attempt, path, "
                "console_session_id, decision, reason, decided_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                [
                    (
                        sprint_session_id,
                        attempt,
                        d.path,
                        console_session_id,
                        d.decision,
                        d.reason,
                        d.decided_at,
                    )
                    for d in decisions
                ],
            )

    def close_review(
        self,
        console_session_id: str,
        sprint_session_id: str,
        attempt: int,
        *,
        status: str,
        decided_at: str,
    ) -> None:
        """Close a review, but only while it is still pending.

        The guard is what lets the gate node abandon a review in its ``finally`` without
        having to know whether the API already decided it — an abandoned run and a decided
        one race otherwise, and the loser would overwrite the real outcome.
        """
        with closing(self._connect()) as conn, conn:
            conn.execute(
                "UPDATE diff_reviews SET status = ?, decided_at = ? WHERE console_session_id = ? "
                "AND sprint_session_id = ? AND attempt = ? AND status = 'pending'",
                (status, decided_at, console_session_id, sprint_session_id, attempt),
            )

    def delete_for_console_session(self, console_session_id: str) -> None:
        with closing(self._connect()) as conn, conn:
            for table in ("diff_files", "diff_snapshots", "diff_reviews", "diff_decisions"):
                conn.execute(
                    f"DELETE FROM {table} WHERE console_session_id = ?", (console_session_id,)
                )

    def clear(self) -> None:
        with closing(self._connect()) as conn, conn:
            for table in ("diff_files", "diff_snapshots", "diff_reviews", "diff_decisions"):
                conn.execute(f"DELETE FROM {table}")

    def _snapshot_from_row(
        self, conn: sqlite3.Connection, row: sqlite3.Row
    ) -> WorkspaceDiffSnapshot:
        """Takes the caller's connection: the file rows belong to the same logical read, and
        opening a second one made every snapshot fetch cost two connections."""
        file_rows = conn.execute(
            f"SELECT {_SUMMARY_COLUMNS} FROM diff_files "
            "WHERE sprint_session_id = ? AND attempt = ? ORDER BY path ASC",
            (row["sprint_session_id"], row["attempt"]),
        ).fetchall()
        return WorkspaceDiffSnapshot(
            console_session_id=row["console_session_id"],
            sprint_session_id=row["sprint_session_id"],
            ticket_key=row["ticket_key"],
            attempt=int(row["attempt"]),
            git_sha=row["git_sha"],
            captured_at=row["captured_at"],
            files=[DiffFileSummary(**_summary_fields(f)) for f in file_rows],
            total_additions=int(row["total_additions"]),
            total_deletions=int(row["total_deletions"]),
            truncated=bool(row["truncated"]),
        )

    def _review_from_row(self, conn: sqlite3.Connection, row: sqlite3.Row) -> DiffReviewState:
        """Takes the caller's connection, like ``_snapshot_from_row``: the decisions and the
        snapshot's file list belong to the same logical read."""
        key = (row["sprint_session_id"], row["attempt"])
        decisions = [
            FileDecision(
                path=d["path"],
                decision=d["decision"],
                reason=d["reason"],
                decided_at=d["decided_at"],
            )
            for d in conn.execute(
                "SELECT path, decision, reason, decided_at FROM diff_decisions "
                "WHERE sprint_session_id = ? AND attempt = ? ORDER BY path ASC",
                key,
            ).fetchall()
        ]
        decided = {d.path for d in decisions}
        undecided = [
            f["path"]
            for f in conn.execute(
                "SELECT path FROM diff_files WHERE sprint_session_id = ? AND attempt = ? "
                "ORDER BY path ASC",
                key,
            ).fetchall()
            if f["path"] not in decided
        ]
        return DiffReviewState(
            sprint_session_id=row["sprint_session_id"],
            attempt=int(row["attempt"]),
            rejection_round=int(row["rejection_round"]),
            status=row["status"],
            requested_at=row["requested_at"],
            expires_at=row["expires_at"],
            decided_at=row["decided_at"],
            decisions=decisions,
            undecided_paths=undecided,
        )


def _summary_fields(row: sqlite3.Row) -> dict[str, object]:
    return {
        "path": row["path"],
        "old_path": row["old_path"],
        "action": row["action"],
        "additions": int(row["additions"]),
        "deletions": int(row["deletions"]),
        "binary": bool(row["binary"]),
        "truncated": bool(row["truncated"]),
    }


def diff_store() -> DiffStore:
    # Cached like the other stores: constructing one runs four DDL statements, and the
    # factory is called from the persistence seam *outside* the to_thread that wraps the
    # query — an uncached store put a connect + CREATE TABLE on the event loop per request.
    return _cached_store(DiffStore, get_settings().session_db)
