from __future__ import annotations

from unittest.mock import MagicMock, patch

from sprint_crew.vector.search import SearchHit, format_search_hits, semantic_search


def test_format_search_hits_empty() -> None:
    assert format_search_hits([]) == "(no semantic matches)"


def test_format_search_hits_includes_path_and_score() -> None:
    hits = [
        SearchHit(
            path="src/auth.py",
            start_line=1,
            end_line=10,
            score=0.91,
            chunk_kind="code",
            snippet="def auth(): pass",
        )
    ]
    text = format_search_hits(hits)
    assert "src/auth.py" in text
    assert "0.910" in text


@patch("sprint_crew.vector.search.get_settings")
@patch("sprint_crew.vector.search.embed_texts")
@patch("sprint_crew.vector.search.QdrantStore")
def test_semantic_search_filters_path_prefix(
    mock_store_cls: MagicMock,
    mock_embed: MagicMock,
    mock_settings: MagicMock,
) -> None:
    mock_settings.return_value.vector_index_enabled = True
    mock_settings.return_value.vector_top_k = 8
    mock_settings.return_value.vector_score_threshold = 0.55
    mock_embed.return_value = [[0.1, 0.2]]

    point_ok = MagicMock()
    point_ok.score = 0.9
    point_ok.payload = {
        "path": "src/auth.py",
        "start_line": 1,
        "end_line": 5,
        "chunk_kind": "code",
        "text": "auth middleware",
    }
    point_skip = MagicMock()
    point_skip.score = 0.95
    point_skip.payload = {
        "path": "tests/test_auth.py",
        "start_line": 1,
        "end_line": 5,
        "chunk_kind": "test",
        "text": "test auth",
    }
    mock_store_cls.return_value.search.return_value = [point_ok, point_skip]

    hits = semantic_search("sess-1", "authentication", path_prefix="src/")
    assert len(hits) == 1
    assert hits[0].path == "src/auth.py"
