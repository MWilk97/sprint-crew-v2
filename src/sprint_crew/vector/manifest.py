"""What each collection currently holds, so a re-index can be a delta (roadmap M8).

Indexing used to be all-or-nothing on ``HEAD``: same sha, skip everything; different sha,
delete the collection and rebuild it. That made a one-line commit cost a full re-embed and
left the index stale for the whole of a Coder run. Incremental re-index needs the previous
per-file state, and Qdrant can only answer that with a full scroll — so the answer is kept
here instead, in the same sqlite file as the other stores.

The diffing itself is :func:`plan_reindex`, a pure function over two mappings: unit tests
exercise it with neither Qdrant nor the embed sidecar running (invariant 10).
"""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path

from sprint_crew.config import get_settings

_CREATE_FILES_SQL = """
    CREATE TABLE IF NOT EXISTS index_files (
        collection TEXT NOT NULL,
        path TEXT NOT NULL,
        content_hash TEXT NOT NULL,
        chunk_count INTEGER NOT NULL,
        PRIMARY KEY (collection, path)
    )
"""
_CREATE_COLLECTIONS_SQL = """
    CREATE TABLE IF NOT EXISTS index_collections (
        collection TEXT PRIMARY KEY,
        repo_key TEXT NOT NULL,
        git_sha TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        last_used_at TEXT NOT NULL
    )
"""


def _utc_now_iso() -> str:
    return datetime.now(tz=UTC).isoformat()


@dataclass(frozen=True)
class ReindexPlan:
    added: tuple[str, ...]
    changed: tuple[str, ...]
    deleted: tuple[str, ...]

    @property
    def is_empty(self) -> bool:
        return not (self.added or self.changed or self.deleted)

    @property
    def to_embed(self) -> tuple[str, ...]:
        """Files needing fresh chunks and vectors."""
        return self.added + self.changed

    @property
    def to_drop(self) -> tuple[str, ...]:
        """Files whose existing points must go. A changed file is dropped *and* re-added:
        re-chunking can yield fewer chunks, and an upsert alone would orphan the surplus."""
        return self.changed + self.deleted


def plan_reindex(old: Mapping[str, str], new: Mapping[str, str]) -> ReindexPlan:
    """Diff two ``path -> content_hash`` mappings into the work an index needs."""
    return ReindexPlan(
        added=tuple(sorted(p for p in new if p not in old)),
        changed=tuple(sorted(p for p in new if p in old and old[p] != new[p])),
        deleted=tuple(sorted(p for p in old if p not in new)),
    )


class IndexManifest:
    """Per-collection file hashes plus collection-level bookkeeping for LRU eviction."""

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as conn, conn:
            conn.execute(_CREATE_FILES_SQL)
            conn.execute(_CREATE_COLLECTIONS_SQL)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    def file_hashes(self, collection: str) -> dict[str, str]:
        with closing(self._connect()) as conn, conn:
            rows = conn.execute(
                "SELECT path, content_hash FROM index_files WHERE collection = ?",
                (collection,),
            ).fetchall()
        return {str(row["path"]): str(row["content_hash"]) for row in rows}

    def apply(
        self,
        collection: str,
        *,
        repo_key: str,
        git_sha: str,
        upserted: Mapping[str, tuple[str, int]],
        deleted: tuple[str, ...],
    ) -> None:
        """Record one re-index pass: ``upserted`` is ``path -> (content_hash, chunk_count)``."""
        now = _utc_now_iso()
        with closing(self._connect()) as conn, conn:
            if deleted:
                conn.executemany(
                    "DELETE FROM index_files WHERE collection = ? AND path = ?",
                    [(collection, path) for path in deleted],
                )
            if upserted:
                conn.executemany(
                    "INSERT INTO index_files (collection, path, content_hash, chunk_count) "
                    "VALUES (?, ?, ?, ?) ON CONFLICT(collection, path) DO UPDATE SET "
                    "content_hash=excluded.content_hash, chunk_count=excluded.chunk_count",
                    [
                        (collection, path, content_hash, chunk_count)
                        for path, (content_hash, chunk_count) in upserted.items()
                    ],
                )
            conn.execute(
                "INSERT INTO index_collections "
                "(collection, repo_key, git_sha, updated_at, last_used_at) "
                "VALUES (?, ?, ?, ?, ?) ON CONFLICT(collection) DO UPDATE SET "
                "repo_key=excluded.repo_key, git_sha=excluded.git_sha, "
                "updated_at=excluded.updated_at, last_used_at=excluded.last_used_at",
                (collection, repo_key, git_sha, now, now),
            )

    def touch(self, collection: str) -> None:
        """Mark a collection as used now, so LRU eviction reflects reads and not just writes."""
        with closing(self._connect()) as conn, conn:
            conn.execute(
                "UPDATE index_collections SET last_used_at = ? WHERE collection = ?",
                (_utc_now_iso(), collection),
            )

    def chunk_count(self, collection: str) -> int:
        with closing(self._connect()) as conn, conn:
            row = conn.execute(
                "SELECT COALESCE(SUM(chunk_count), 0) AS total FROM index_files "
                "WHERE collection = ?",
                (collection,),
            ).fetchone()
        return int(row["total"]) if row else 0

    def known_collections(self) -> list[str]:
        with closing(self._connect()) as conn, conn:
            rows = conn.execute("SELECT collection FROM index_collections").fetchall()
        return [str(row["collection"]) for row in rows]

    def evictable(self, *, keep: int) -> list[str]:
        """Collections beyond the ``keep`` most recently used, oldest first."""
        with closing(self._connect()) as conn, conn:
            # rowid breaks ties: two writes inside the same microsecond would otherwise
            # order arbitrarily, and eviction would pick between them at random.
            rows = conn.execute(
                "SELECT collection FROM index_collections ORDER BY last_used_at DESC, rowid DESC",
            ).fetchall()
        return [str(row["collection"]) for row in rows[keep:]][::-1]

    def forget(self, collection: str) -> None:
        with closing(self._connect()) as conn, conn:
            conn.execute("DELETE FROM index_files WHERE collection = ?", (collection,))
            conn.execute("DELETE FROM index_collections WHERE collection = ?", (collection,))

    def clear(self) -> None:
        with closing(self._connect()) as conn, conn:
            conn.execute("DELETE FROM index_files")
            conn.execute("DELETE FROM index_collections")


@lru_cache(maxsize=4)
def _manifest_for(db_path: Path) -> IndexManifest:
    # One instance per database file: constructing one runs the CREATE TABLE statements,
    # so an uncached factory would pay two connections per call. Holds a path, not a
    # connection, so sharing it across threads is safe — same contract as _cached_store.
    return IndexManifest(db_path)


def index_manifest() -> IndexManifest:
    return _manifest_for(get_settings().session_db)
