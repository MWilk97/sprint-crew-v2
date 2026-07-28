from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from qdrant_client.http.models import ScoredPoint

from sprint_crew.config import get_settings
from sprint_crew.vector.embeddings import embed_texts
from sprint_crew.vector.store import QdrantStore


@dataclass(frozen=True)
class SearchHit:
    path: str
    start_line: int
    end_line: int
    score: float
    chunk_kind: str
    snippet: str


def semantic_search(
    collections: Sequence[str],
    query: str,
    *,
    top_k: int | None = None,
    path_prefix: str | None = None,
    chunk_kind: str | None = None,
) -> list[SearchHit]:
    """Search one or more collections, earlier ones winning per file (roadmap M8).

    Callers pass ``(run overlay, shared repo index)``: a file the running agent has edited
    is only accurate in the overlay, so any hit the overlay returns for a path suppresses
    that path's committed-state hits. Files the run has not touched still come from the
    shared index, which is the whole point of keeping one.
    """
    settings = get_settings()
    if not settings.vector_index_enabled:
        return []
    if not query.strip():
        return []

    k = top_k if top_k is not None else settings.vector_top_k
    vectors = embed_texts([query.strip()], input_type="query")
    if not vectors:
        return []

    store = QdrantStore()
    hits: list[SearchHit] = []
    superseded: set[str] = set()
    for collection in collections:
        scored = store.search(collection, vectors[0], top_k=k, chunk_kind=chunk_kind)
        collection_hits = [
            hit
            for hit in (_hit_from_point(point) for point in scored)
            if hit.path not in superseded and _matches_prefix(hit.path, path_prefix)
        ]
        hits.extend(collection_hits)
        superseded.update(hit.path for hit in collection_hits)

    hits.sort(key=lambda hit: hit.score, reverse=True)
    return hits[:k]


def _matches_prefix(path: str, path_prefix: str | None) -> bool:
    if not path_prefix:
        return True
    return path.startswith(path_prefix.lstrip("./"))


def _hit_from_point(point: ScoredPoint) -> SearchHit:
    payload = point.payload or {}
    text = str(payload.get("text", ""))
    return SearchHit(
        path=str(payload.get("path", "")),
        start_line=int(payload.get("start_line", 0)),
        end_line=int(payload.get("end_line", 0)),
        score=float(point.score or 0.0),
        chunk_kind=str(payload.get("chunk_kind", "")),
        snippet=text if len(text) <= 500 else text[:500] + "…",
    )


def format_search_hits(hits: list[SearchHit]) -> str:
    if not hits:
        return "(no semantic matches)"
    lines: list[str] = []
    for hit in hits:
        lines.append(
            f"{hit.path}:{hit.start_line}-{hit.end_line} "
            f"(score={hit.score:.3f}, kind={hit.chunk_kind})\n{hit.snippet}"
        )
    return "\n\n---\n\n".join(lines)
