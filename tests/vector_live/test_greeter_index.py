from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest

from sprint_crew.vector.indexer import index_workspace
from sprint_crew.vector.search import semantic_search


@pytest.mark.vector_live
def test_greeter_fixture_index_round_trip(
    skip_unless_vector_live, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("VECTOR_INDEX_ENABLED", "1")
    from sprint_crew.config import get_settings

    get_settings.cache_clear()

    root = Path(__file__).resolve().parents[2] / "fixtures" / "repo"
    session_id = f"greeter-vector-live-{uuid4().hex[:8]}"
    result = index_workspace(root, session_id)
    assert result.chunks > 0
    hits = semantic_search(session_id, "hello greeting", top_k=3)
    assert hits or result.chunks < 3
