from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

from sprint_crew.tools.semantic_search import SemanticSearchArgs, SemanticSearchTool
from sprint_crew.vector.chunker import chunk_file, count_indexable_files, iter_workspace_chunks
from sprint_crew.vector.search import SearchHit, format_search_hits, semantic_search


def test_python_chunk_by_function(tmp_path: Path) -> None:
    src = tmp_path / "greeter.py"
    src.write_text(
        "def hello():\n    return 'hello'\n\n\ndef goodbye():\n    return 'bye'\n",
        encoding="utf-8",
    )
    chunks = chunk_file("greeter.py", src.read_text(encoding="utf-8"))
    assert len(chunks) >= 2
    assert all(c.path == "greeter.py" for c in chunks)
    assert any("hello" in c.text for c in chunks)


def test_skips_forbidden_paths(tmp_path: Path) -> None:
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "config").write_text("secret\n", encoding="utf-8")
    (tmp_path / "main.py").write_text("x = 1\n", encoding="utf-8")
    assert count_indexable_files(tmp_path) == 1
    chunks = iter_workspace_chunks(tmp_path)
    assert len(chunks) == 1
    assert chunks[0].path == "main.py"


def test_chunk_display_includes_path_header(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("def f():\n    pass\n", encoding="utf-8")
    chunks = iter_workspace_chunks(tmp_path)
    assert chunks
    display = chunks[0].display_text()
    assert "# path: a.py" in display
    assert "# kind:" in display


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


def test_semantic_search_unavailable_when_disabled(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("VECTOR_INDEX_ENABLED", "false")
    from sprint_crew.config import get_settings

    get_settings.cache_clear()

    tool = SemanticSearchTool(session_id="sess-1")
    result = tool.execute(
        SemanticSearchArgs(query="hello"),
        workspace_root=tmp_path,
    )
    assert result.ok is True
    assert "unavailable" in result.output


def test_semantic_search_uses_session_id(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("VECTOR_INDEX_ENABLED", "true")
    from sprint_crew.config import get_settings

    get_settings.cache_clear()

    calls: list[str] = []

    def fake_search(session_id: str, query: str, **kwargs):
        calls.append(session_id)
        return []

    monkeypatch.setattr(
        "sprint_crew.tools.semantic_search.semantic_search",
        fake_search,
    )

    tool = SemanticSearchTool(session_id="explicit-id")
    tool.execute(SemanticSearchArgs(query="auth"), workspace_root=tmp_path)
    assert calls == ["explicit-id"]


def test_semantic_search_rejects_unsafe_path_prefix(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("VECTOR_INDEX_ENABLED", "true")
    from sprint_crew.config import get_settings

    get_settings.cache_clear()

    tool = SemanticSearchTool(session_id="sess-1")
    result = tool.execute(
        SemanticSearchArgs(query="hello", path_prefix="../.git"),
        workspace_root=tmp_path,
    )
    assert result.ok is False
    assert result.error == "unsafe path"
