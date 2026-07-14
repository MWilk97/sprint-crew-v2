from __future__ import annotations

import hashlib
import re
import uuid
from typing import Any

from qdrant_client import QdrantClient
from qdrant_client.http import models as qmodels

from sprint_crew.config import get_settings
from sprint_crew.vector.chunker import CodeChunk

_SESSION_SAFE_RE = re.compile(r"[^a-zA-Z0-9_-]+")


def _sanitize_session_id(session_id: str) -> str:
    cleaned = _SESSION_SAFE_RE.sub("_", session_id.strip())
    return cleaned[:128] or "default"


def collection_name(session_id: str) -> str:
    settings = get_settings()
    safe = _sanitize_session_id(session_id)
    return f"{settings.qdrant_collection_prefix}_{safe}"


def point_id(session_id: str, chunk: CodeChunk) -> str:
    raw = f"{session_id}:{chunk.path}:{chunk.start_line}:{chunk.end_line}"
    digest = hashlib.sha256(raw.encode()).hexdigest()
    return str(uuid.UUID(digest[:32]))


class QdrantStore:
    def __init__(self, url: str | None = None) -> None:
        settings = get_settings()
        self._client = QdrantClient(url=url or settings.qdrant_url)

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

    def get_collection_git_sha(self, name: str) -> str | None:
        """Return stored git SHA when collection exists and has at least one point."""
        if not self._client.collection_exists(name):
            return None
        points, _ = self._client.scroll(
            collection_name=name,
            limit=1,
            with_payload=True,
            with_vectors=False,
        )
        if not points:
            return None
        payload = points[0].payload or {}
        sha = payload.get("git_sha")
        return str(sha) if sha else None

    def upsert_chunks(
        self,
        collection: str,
        session_id: str,
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
                "session_id": session_id,
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
                    id=point_id(session_id, chunk),
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
