from __future__ import annotations

from sprint_crew.agents.tech_lead import _gather_repo_context, _paths_from_ticket
from sprint_crew.schemas.ticket import JiraTicket


def test_paths_from_ticket_extracts_greeter_py() -> None:
    ticket = JiraTicket(
        key="DEMO-1",
        summary="Add hello() to greeter.py",
        description="Implement hello() in greeter.py with pytest.",
        status="To Do",
        issue_type="Story",
        acceptance_criteria="pytest -q tests/test_greeter.py passes",
    )
    paths = _paths_from_ticket(ticket)
    assert "greeter.py" in paths
    assert "tests/test_greeter.py" in paths


def test_gather_repo_context_includes_git_status_and_greeter(tmp_workspace) -> None:
    (tmp_workspace / "greeter.py").write_text(
        "def hello():\n    return 'hello'\n", encoding="utf-8"
    )
    ticket = JiraTicket(
        key="DEMO-1",
        summary="Add hello() to greeter.py",
        description="Implement hello() returning 'hello'.",
        status="To Do",
        issue_type="Story",
        acceptance_criteria="- tests pass",
    )
    context = _gather_repo_context(tmp_workspace, ticket)
    assert "=== git status ===" in context
    assert "=== directory listing" in context
    assert "=== read_file: greeter.py ===" in context
    assert "hello" in context


def test_enrich_repo_context_appends_semantic_section(tmp_workspace, monkeypatch) -> None:
    monkeypatch.setenv("VECTOR_INDEX_ENABLED", "true")
    from sprint_crew.config import get_settings

    get_settings.cache_clear()

    monkeypatch.setattr(
        "sprint_crew.vector.context.should_index_workspace",
        lambda *a, **k: True,
    )
    monkeypatch.setattr(
        "sprint_crew.vector.context.semantic_search",
        lambda *a, **k: [
            __import__("sprint_crew.vector.search", fromlist=["SearchHit"]).SearchHit(
                path="auth.py",
                start_line=1,
                end_line=5,
                score=0.88,
                chunk_kind="code",
                snippet="def auth(): ...",
            )
        ],
    )

    from sprint_crew.vector.context import enrich_repo_context

    text = enrich_repo_context(tmp_workspace, "sess-1", "authentication middleware")
    assert "=== repo_manifest" in text
    assert "=== pre_search" in text
    assert text.count("auth.py") == 1
    assert "auth.py" in text


def test_enrich_repo_context_with_hits_returns_hit_list(tmp_workspace, monkeypatch) -> None:
    monkeypatch.setenv("VECTOR_INDEX_ENABLED", "true")
    from sprint_crew.config import get_settings

    get_settings.cache_clear()

    hit = __import__("sprint_crew.vector.search", fromlist=["SearchHit"]).SearchHit(
        path="auth.py",
        start_line=1,
        end_line=5,
        score=0.88,
        chunk_kind="code",
        snippet="def auth(): ...",
    )
    monkeypatch.setattr(
        "sprint_crew.vector.context.should_index_workspace",
        lambda *a, **k: True,
    )
    monkeypatch.setattr(
        "sprint_crew.vector.context.semantic_search",
        lambda *a, **k: [hit],
    )

    from sprint_crew.vector.context import enrich_repo_context_with_hits, pre_search_agent_event

    text, hits = enrich_repo_context_with_hits(tmp_workspace, "sess-1", "authentication")
    assert hits == [hit]
    assert "auth.py" in text
    event = pre_search_agent_event("authentication", hits)
    assert event.event_type == "pre_search"
    assert event.detail["hits"] == 1
