from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest

from sprint_crew.vector.indexer import index_workspace
from sprint_crew.vector.search import semantic_search


@pytest.mark.vector_live
@pytest.mark.parametrize(
    ("fixture_name", "query", "min_chunks", "assert_fn"),
    [
        (
            "vector_repo",
            "ferry dispatch outbound queue",
            20,
            lambda hits, _result: any("ferry" in hit.path for hit in hits),
        ),
    ],
    ids=["vector_repo_ferry"],
)
def test_fixture_index_round_trip(
    skip_unless_vector_live,
    monkeypatch: pytest.MonkeyPatch,
    fixture_name: str,
    query: str,
    min_chunks: int,
    assert_fn,
) -> None:
    monkeypatch.setenv("VECTOR_INDEX_ENABLED", "1")
    from sprint_crew.config import get_settings

    get_settings.cache_clear()

    root = Path(__file__).resolve().parents[2] / "fixtures" / fixture_name
    session_id = f"{fixture_name}-vector-live-{uuid4().hex[:8]}"
    result = index_workspace(root, session_id)
    assert result.chunks >= min_chunks

    hits = semantic_search(session_id, query, top_k=5)
    assert assert_fn(hits, result), (
        f"assertion failed for {fixture_name}: {[(h.path, h.score) for h in hits]}"
    )


@pytest.mark.vector_live
def test_vector_repo_surfaces_retry_hits(
    skip_unless_vector_live,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("VECTOR_INDEX_ENABLED", "1")
    from sprint_crew.config import get_settings

    get_settings.cache_clear()

    root = Path(__file__).resolve().parents[2] / "fixtures" / "vector_repo"
    session_id = f"vector-repo-retry-{uuid4().hex[:8]}"
    index_workspace(root, session_id)

    retry_hits = semantic_search(session_id, "exponential backoff retry adapter", top_k=5)
    assert retry_hits, "expected retry-related hits in vector_repo fixture"
