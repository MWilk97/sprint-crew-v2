from __future__ import annotations

import hashlib
import re
import uuid
from functools import lru_cache
from typing import Any

from qdrant_client import QdrantClient
from qdrant_client.http import models as qmodels

from sprint_crew.config import get_settings
from sprint_crew.vector.chunker import CodeChunk

_NAME_SAFE_RE = re.compile(r"[^a-zA-Z0-9_-]+")
_SCHEME_RE = re.compile(r"^[a-z][a-z0-9+.-]*://")
_USERINFO_RE = re.compile(r"^[^/@]+@")

REPO_COLLECTION_INFIX = "repo"
RUN_COLLECTION_INFIX = "run"


def _sanitize(value: str) -> str:
    cleaned = _NAME_SAFE_RE.sub("_", value.strip())
    return cleaned[:128] or "default"


def normalize_repo_url(repo_url: str | None) -> str:
    """One spelling for one repository.

    ``https://host/o/r.git``, ``git@host:o/r`` and ``ssh://git@host/o/r/`` are the same
    remote addressed three ways; without collapsing them the same repo would index three
    times and share nothing. A session with no ``repo_url`` runs against the bundled
    fixture, which is a single logical repo too.
    """
    if not repo_url or not repo_url.strip():
        return "fixture"
    text = repo_url.strip().lower()
    text = _SCHEME_RE.sub("", text)
    text = _USERINFO_RE.sub("", text)
    # scp-style ``host:owner/repo`` — the colon is a path separator, not a port here.
    text = text.replace(":", "/")
    text = text.rstrip("/")
    return text.removesuffix(".git").rstrip("/") or "fixture"


def repo_key(repo_url: str | None) -> str:
    """Stable short identity for a repository, safe to put in a collection name."""
    return hashlib.sha256(normalize_repo_url(repo_url).encode()).hexdigest()[:16]


def collection_for_repo(key: str) -> str:
    """The shared, durable collection holding a repo's committed state (roadmap M8)."""
    return f"{get_settings().qdrant_collection_prefix}_{REPO_COLLECTION_INFIX}_{_sanitize(key)}"


def collection_for_run(run_scope_id: str) -> str:
    """A run's dirty overlay: uncommitted edits, dropped when the run ends.

    Kept out of the shared collection so one run's work-in-progress is never visible to
    another session, and so a cancelled run leaves nothing behind to clean up.
    """
    return f"{get_settings().qdrant_collection_prefix}_{RUN_COLLECTION_INFIX}_{_sanitize(run_scope_id)}"


def point_id(scope: str, chunk: CodeChunk) -> str:
    """Deterministic id for a chunk within a collection.

    Scoped by collection rather than by session: incremental re-index upserts over the
    same ids, so they must not move when a different session indexes the same repo.
    """
    raw = f"{scope}:{chunk.path}:{chunk.start_line}:{chunk.end_line}"
    digest = hashlib.sha256(raw.encode()).hexdigest()
    return str(uuid.UUID(digest[:32]))


@lru_cache(maxsize=4)
def _client_for_url(url: str) -> QdrantClient:
    return QdrantClient(url=url)


class QdrantStore:
    def __init__(self, url: str | None = None) -> None:
        settings = get_settings()
        self._client = _client_for_url(url or settings.qdrant_url)

    def ensure_collection(self, name: str, vector_size: int) -> None:
        exists = self._client.collection_exists(name)
        if exists:
            return
        self._client.create_collection(
            collection_name=name,
            vectors_config=qmodels.VectorParams(
                size=vector_size,
                distance=qmodels.Distance.COSINE,
            ),
        )

    def delete_collection(self, name: str) -> None:
        if self._client.collection_exists(name):
            self._client.delete_collection(name)

    def list_collections(self, prefix: str) -> list[str]:
        response = self._client.get_collections()
        return [c.name for c in response.collections if c.name.startswith(prefix)]

    def delete_by_paths(self, name: str, paths: list[str]) -> None:
        """Drop every chunk of the given files. The unit of incremental re-index:
        a changed file's old chunks must go before the new ones land, because
        re-chunking can produce fewer chunks and orphans would survive an upsert."""
        if not paths or not self._client.collection_exists(name):
            return
        self._client.delete(
            collection_name=name,
            points_selector=qmodels.FilterSelector(
                filter=qmodels.Filter(
                    must=[
                        qmodels.FieldCondition(
                            key="path",
                            match=qmodels.MatchAny(any=list(paths)),
                        )
                    ]
                )
            ),
        )

    def upsert_chunks(
        self,
        collection: str,
        chunks: list[CodeChunk],
        vectors: list[list[float]],
        *,
        git_sha: str = "",
    ) -> None:
        if len(chunks) != len(vectors):
            raise ValueError("chunks and vectors length mismatch")
        if not chunks:
            return
        self.ensure_collection(collection, len(vectors[0]))
        points = []
        for chunk, vector in zip(chunks, vectors, strict=True):
            text = chunk.display_text()
            payload: dict[str, Any] = {
                "path": chunk.path,
                "start_line": chunk.start_line,
                "end_line": chunk.end_line,
                "chunk_kind": chunk.chunk_kind,
                "language": chunk.language,
                "git_sha": git_sha,
                "text": text,
            }
            points.append(
                qmodels.PointStruct(
                    id=point_id(collection, chunk),
                    vector=vector,
                    payload=payload,
                )
            )
        self._client.upsert(collection_name=collection, points=points)

    def search(
        self,
        collection: str,
        query_vector: list[float],
        *,
        top_k: int,
        chunk_kind: str | None = None,
        score_threshold: float | None = None,
    ) -> list[qmodels.ScoredPoint]:
        if not self._client.collection_exists(collection):
            return []

        must: list[qmodels.FieldCondition] = []
        if chunk_kind:
            must.append(
                qmodels.FieldCondition(
                    key="chunk_kind",
                    match=qmodels.MatchValue(value=chunk_kind),
                )
            )

        query_filter = qmodels.Filter(must=must) if must else None
        settings = get_settings()
        threshold = (
            score_threshold if score_threshold is not None else settings.vector_score_threshold
        )

        response = self._client.query_points(
            collection_name=collection,
            query=query_vector,
            limit=top_k,
            query_filter=query_filter,
            score_threshold=threshold,
        )
        return list(response.points)
