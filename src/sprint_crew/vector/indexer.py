from __future__ import annotations

import logging
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from sprint_crew.config import get_settings
from sprint_crew.vector.chunker import count_indexable_files, iter_workspace_chunks
from sprint_crew.vector.embeddings import embed_texts
from sprint_crew.vector.store import QdrantStore, collection_name

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class IndexResult:
    session_id: str
    collection: str
    files: int
    chunks: int
    seconds: float
    git_sha: str


def _git_head_sha(workspace: Path) -> str:
    proc = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=workspace,
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        return ""
    return (proc.stdout or "").strip()


def index_workspace(workspace: Path, session_id: str) -> IndexResult:
    settings = get_settings()
    if not settings.vector_index_enabled:
        raise RuntimeError("Vector indexing is disabled.")

    started = time.perf_counter()
    root = workspace.resolve()
    file_count = count_indexable_files(root)
    git_sha = _git_head_sha(root)
    coll = collection_name(session_id)

    store = QdrantStore()
    if git_sha and store.get_collection_git_sha(coll) == git_sha:
        elapsed = time.perf_counter() - started
        log.info(
            "Skipping re-index for %s — git SHA unchanged (%s)",
            session_id,
            git_sha[:8],
        )
        return IndexResult(
            session_id=session_id,
            collection=coll,
            files=file_count,
            chunks=-1,
            seconds=elapsed,
            git_sha=git_sha,
        )

    chunks = iter_workspace_chunks(root)
    store.delete_collection(coll)

    if not chunks:
        elapsed = time.perf_counter() - started
        return IndexResult(
            session_id=session_id,
            collection=coll,
            files=file_count,
            chunks=0,
            seconds=elapsed,
            git_sha=git_sha,
        )

    texts = [c.display_text() for c in chunks]
    vectors = embed_texts(texts, input_type="passage")
    store.upsert_chunks(coll, session_id, chunks, vectors, git_sha=git_sha)

    elapsed = time.perf_counter() - started
    log.info(
        "Indexed workspace %s: %d chunks from %d files in %.2fs",
        session_id,
        len(chunks),
        file_count,
        elapsed,
    )
    return IndexResult(
        session_id=session_id,
        collection=coll,
        files=file_count,
        chunks=len(chunks),
        seconds=elapsed,
        git_sha=git_sha,
    )


def delete_workspace_index(session_id: str) -> None:
    """Drop a session's collection; no-op when the vector index is disabled."""
    if not get_settings().vector_index_enabled:
        return
    store = QdrantStore()
    store.delete_collection(collection_name(session_id))
