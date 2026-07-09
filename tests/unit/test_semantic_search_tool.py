from __future__ import annotations

from pathlib import Path

from sprint_crew.tools.semantic_search import SemanticSearchArgs, SemanticSearchTool


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
