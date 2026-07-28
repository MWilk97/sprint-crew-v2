from __future__ import annotations

import logging
import subprocess
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

from sprint_crew.config import get_settings
from sprint_crew.vector.chunker import CodeChunk, chunks_for_paths, file_hashes
from sprint_crew.vector.embeddings import embed_texts
from sprint_crew.vector.manifest import index_manifest, plan_reindex
from sprint_crew.vector.store import QdrantStore

log = logging.getLogger(__name__)

# Chunks per embed request. Bounds the sidecar payload on a first-time index of a real
# repo (thousands of chunks) and gives progress something to report between batches.
_EMBED_BATCH = 128

ProgressFn = Callable[[int, int], None]


@dataclass(frozen=True)
class IndexResult:
    collection: str
    files: int
    chunks: int
    seconds: float
    git_sha: str
    added: int = 0
    changed: int = 0
    deleted: int = 0

    @property
    def unchanged(self) -> bool:
        """Nothing was re-embedded — the index already matched the workspace."""
        return not (self.added or self.changed or self.deleted)


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


def index_workspace(
    workspace: Path,
    collection: str,
    *,
    repo_key: str,
    hashes: Mapping[str, str] | None = None,
    on_progress: ProgressFn | None = None,
) -> IndexResult:
    """Bring ``collection`` in line with the workspace, re-embedding only what changed.

    ``hashes`` is the target content of the collection as ``path -> content hash``; it
    defaults to the whole workspace. An overlay passes a subset, so the collection holds
    exactly the currently-differing files and one that stops differing is dropped on the
    next pass by virtue of being absent. Callers that have already hashed the tree pass
    the result rather than making this walk it a second time.
    """
    settings = get_settings()
    if not settings.vector_index_enabled:
        raise RuntimeError("Vector indexing is disabled.")

    started = time.perf_counter()
    root = workspace.resolve()
    git_sha = _git_head_sha(root)

    if hashes is None:
        hashes = file_hashes(root)

    manifest = index_manifest()
    plan = plan_reindex(manifest.file_hashes(collection), hashes)
    store = QdrantStore()

    if plan.is_empty:
        manifest.touch(collection)
        return IndexResult(
            collection=collection,
            files=len(hashes),
            chunks=0,
            seconds=time.perf_counter() - started,
            git_sha=git_sha,
        )

    # Drop before adding: re-chunking a changed file can produce fewer chunks than it had,
    # and an upsert only overwrites the ids it writes — the surplus would survive as stale
    # hits pointing at lines that no longer exist.
    store.delete_by_paths(collection, list(plan.to_drop))

    chunked = chunks_for_paths(root, plan.to_embed)
    flat: list[CodeChunk] = [chunk for chunks in chunked.values() for chunk in chunks]
    _embed_and_upsert(store, collection, flat, git_sha=git_sha, on_progress=on_progress)

    manifest.apply(
        collection,
        repo_key=repo_key,
        git_sha=git_sha,
        upserted={
            path: (hashes[path], len(chunked.get(path, [])))
            for path in plan.to_embed
            if path in hashes
        },
        deleted=plan.deleted,
    )

    elapsed = time.perf_counter() - started
    log.info(
        "Indexed %s: +%d ~%d -%d files, %d chunks in %.2fs",
        collection,
        len(plan.added),
        len(plan.changed),
        len(plan.deleted),
        len(flat),
        elapsed,
    )
    return IndexResult(
        collection=collection,
        files=len(hashes),
        chunks=len(flat),
        seconds=elapsed,
        git_sha=git_sha,
        added=len(plan.added),
        changed=len(plan.changed),
        deleted=len(plan.deleted),
    )


def _embed_and_upsert(
    store: QdrantStore,
    collection: str,
    chunks: list[CodeChunk],
    *,
    git_sha: str,
    on_progress: ProgressFn | None,
) -> None:
    total = len(chunks)
    for start in range(0, total, _EMBED_BATCH):
        batch = chunks[start : start + _EMBED_BATCH]
        vectors = embed_texts([c.display_text() for c in batch], input_type="passage")
        store.upsert_chunks(collection, batch, vectors, git_sha=git_sha)
        if on_progress is not None:
            on_progress(min(start + _EMBED_BATCH, total), total)


def delete_index(collection: str) -> None:
    """Drop a collection and forget its manifest; no-op when the vector index is disabled."""
    if not get_settings().vector_index_enabled:
        return
    QdrantStore().delete_collection(collection)
    index_manifest().forget(collection)


def enforce_collection_lru() -> None:
    """Keep at most ``VECTOR_MAX_COLLECTIONS`` indexes on disk, dropping the coldest first.

    Shared repo collections are no longer deleted after a run (that is the milestone), so
    something has to bound them; least-recently-used is the honest policy when the only
    signal is "which repo did someone look at last".
    """
    settings = get_settings()
    if not settings.vector_index_enabled or settings.vector_max_collections <= 0:
        return
    manifest = index_manifest()
    for collection in manifest.evictable(keep=settings.vector_max_collections):
        log.info("Evicting least-recently-used collection %s", collection)
        delete_index(collection)


def sweep_orphan_collections() -> list[str]:
    """Delete prefixed collections the manifest does not know about.

    M8 re-keyed collections from session id to repo, which orphans every collection built
    before it — nothing references them and no code path would ever collect them. Also
    catches an overlay whose run died before its own cleanup ran.
    """
    settings = get_settings()
    if not settings.vector_index_enabled or not settings.vector_orphan_sweep:
        return []
    store = QdrantStore()
    known = set(index_manifest().known_collections())
    orphans = [
        name
        for name in store.list_collections(settings.qdrant_collection_prefix)
        if name not in known
    ]
    for name in orphans:
        log.info("Dropping orphaned collection %s", name)
        store.delete_collection(name)
    return orphans
