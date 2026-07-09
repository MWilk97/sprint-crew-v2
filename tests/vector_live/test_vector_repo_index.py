from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest

from sprint_crew.vector.indexer import index_workspace
from sprint_crew.vector.search import semantic_search


@pytest.mark.vector_live
def test_vector_repo_fixture_surfaces_ferry_on_search(
    skip_unless_vector_live,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("VECTOR_INDEX_ENABLED", "1")
    from sprint_crew.config import get_settings

    get_settings.cache_clear()

    root = Path(__file__).resolve().parents[2] / "fixtures" / "vector_repo"
    session_id = f"vector-repo-live-{uuid4().hex[:8]}"
    result = index_workspace(root, session_id)
    assert result.chunks >= 20

    ferry_hits = semantic_search(session_id, "ferry dispatch outbound queue", top_k=5)
    assert any("ferry" in hit.path for hit in ferry_hits), (
        f"expected ferry.py in hits, got {[(h.path, h.score) for h in ferry_hits]}"
    )

    retry_hits = semantic_search(session_id, "exponential backoff retry adapter", top_k=5)
    assert retry_hits, "expected retry-related hits in vector_repo fixture"
