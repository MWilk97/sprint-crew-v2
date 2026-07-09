from __future__ import annotations

from dataclasses import dataclass

from sprint_crew.config import get_settings
from sprint_crew.vector.embeddings import embed_texts
from sprint_crew.vector.store import QdrantStore, collection_name


@dataclass(frozen=True)
class SearchHit:
    path: str
    start_line: int
    end_line: int
    score: float
    chunk_kind: str
    snippet: str


def semantic_search(
    session_id: str,
    query: str,
    *,
    top_k: int | None = None,
    path_prefix: str | None = None,
    chunk_kind: str | None = None,
) -> list[SearchHit]:
    settings = get_settings()
    if not settings.vector_index_enabled:
        return []
    if not query.strip():
        return []

    k = top_k if top_k is not None else settings.vector_top_k
    coll = collection_name(session_id)
    vectors = embed_texts([query.strip()], input_type="query")
    if not vectors:
        return []

    store = QdrantStore()
    scored = store.search(
        coll,
        vectors[0],
        top_k=k,
        chunk_kind=chunk_kind,
    )

    hits: list[SearchHit] = []
    for point in scored:
        payload = point.payload or {}
        path = str(payload.get("path", ""))
        if path_prefix:
            prefix = path_prefix.lstrip("./")
            if not path.startswith(prefix):
                continue
        text = str(payload.get("text", ""))
        snippet = text if len(text) <= 500 else text[:500] + "…"
        hits.append(
            SearchHit(
                path=path,
                start_line=int(payload.get("start_line", 0)),
                end_line=int(payload.get("end_line", 0)),
                score=float(point.score or 0.0),
                chunk_kind=str(payload.get("chunk_kind", "")),
                snippet=snippet,
            )
        )
    return hits


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
