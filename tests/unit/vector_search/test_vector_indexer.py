from __future__ import annotations

from pathlib import Path

import pytest
from tests.helpers.ticket_fixtures import complex_api_ticket, email_validators_ticket

from sprint_crew.orchestrator.repo_context import should_index_workspace, should_use_vector
from sprint_crew.schemas.ticket import JiraTicket


def _enable_vector(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VECTOR_INDEX_ENABLED", "true")
    from sprint_crew.config import get_settings

    get_settings.cache_clear()


def test_should_use_vector_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VECTOR_INDEX_ENABLED", "false")
    from sprint_crew.config import get_settings

    get_settings.cache_clear()
    assert should_use_vector(ticket=complex_api_ticket()) is False


def test_trivial_ticket_skips_vector(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _enable_vector(monkeypatch)
    ticket = JiraTicket(
        key="DEMO-1",
        summary="Add hello() to greeter module",
        description="Implement hello() returning 'hello'.",
        status="To Do",
        issue_type="Story",
        acceptance_criteria="- Unit tests pass",
    )
    (tmp_path / "greeter.py").write_text("def hello():\n    return 'hello'\n", encoding="utf-8")
    assert should_use_vector(ticket=ticket) is False
    assert should_index_workspace(tmp_path, ticket=ticket) is False


def test_simple_ticket_uses_vector(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _enable_vector(monkeypatch)
    (tmp_path / "validators.py").write_text("pass\n", encoding="utf-8")
    ticket = email_validators_ticket()
    assert should_use_vector(ticket=ticket) is True
    assert should_index_workspace(tmp_path, ticket=ticket) is True


def test_complex_ticket_uses_vector(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _enable_vector(monkeypatch)
    (tmp_path / "main.py").write_text("x=1\n", encoding="utf-8")
    ticket = complex_api_ticket()
    assert should_use_vector(ticket=ticket) is True
    assert should_index_workspace(tmp_path, ticket=ticket) is True


def test_trivial_prompt_skips_vector(monkeypatch: pytest.MonkeyPatch) -> None:
    _enable_vector(monkeypatch)
    assert should_use_vector(prompt="Add hello() to greeter") is False


def test_simple_prompt_uses_vector(monkeypatch: pytest.MonkeyPatch) -> None:
    _enable_vector(monkeypatch)
    assert should_use_vector(prompt="Add validate_email() in validators.py") is True


def test_should_index_requires_indexable_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _enable_vector(monkeypatch)
    ticket = complex_api_ticket()
    assert should_index_workspace(tmp_path, ticket=ticket) is False


@pytest.fixture
def indexed_workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A workspace plus a mocked Qdrant, with the manifest on an isolated database.

    The manifest is durable by design, so without its own db this test would see the
    previous run's state and stop testing anything on the second execution.
    """
    from unittest.mock import MagicMock, patch

    _enable_vector(monkeypatch)
    monkeypatch.setenv("SPRINT_SESSION_DB", str(tmp_path / "manifest.db"))
    from sprint_crew.config import get_settings

    get_settings.cache_clear()

    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "a.py").write_text("x = 1\n", encoding="utf-8")
    (workspace / "b.py").write_text("y = 2\n", encoding="utf-8")

    fake_store = MagicMock()
    embedded: list[list[str]] = []

    def fake_embed(texts, **_kwargs):
        embedded.append(list(texts))
        return [[0.1, 0.2] for _ in texts]

    with (
        patch("sprint_crew.vector.indexer.QdrantStore", return_value=fake_store),
        patch("sprint_crew.vector.indexer.embed_texts", side_effect=fake_embed),
        patch("sprint_crew.vector.indexer._git_head_sha", return_value="abc123"),
    ):
        yield workspace, fake_store, embedded
    get_settings.cache_clear()


def _embedded_paths(batches: list[list[str]]) -> set[str]:
    return {
        line.removeprefix("# path: ")
        for batch in batches
        for text in batch
        for line in text.splitlines()[:1]
    }


def test_index_workspace_reembeds_only_changed_files(indexed_workspace) -> None:
    from sprint_crew.vector.indexer import index_workspace

    workspace, fake_store, embedded = indexed_workspace

    first = index_workspace(workspace, "coll", repo_key="k")
    assert first.added == 2
    assert _embedded_paths(embedded) == {"a.py", "b.py"}

    embedded.clear()
    (workspace / "a.py").write_text("x = 99\n", encoding="utf-8")
    second = index_workspace(workspace, "coll", repo_key="k")

    assert (second.added, second.changed, second.deleted) == (0, 1, 0)
    assert _embedded_paths(embedded) == {"a.py"}
    # The old chunks have to go first: re-chunking can yield fewer of them, and an upsert
    # only overwrites the ids it writes.
    assert fake_store.delete_by_paths.call_args.args[1] == ["a.py"]


def test_index_workspace_is_a_noop_when_nothing_changed(indexed_workspace) -> None:
    from sprint_crew.vector.indexer import index_workspace

    workspace, _fake_store, embedded = indexed_workspace

    index_workspace(workspace, "coll", repo_key="k")
    embedded.clear()

    again = index_workspace(workspace, "coll", repo_key="k")
    assert again.unchanged is True
    assert embedded == []


def test_index_workspace_drops_deleted_files(indexed_workspace) -> None:
    from sprint_crew.vector.indexer import index_workspace

    workspace, fake_store, _embedded = indexed_workspace

    index_workspace(workspace, "coll", repo_key="k")
    (workspace / "b.py").unlink()
    result = index_workspace(workspace, "coll", repo_key="k")

    assert (result.added, result.changed, result.deleted) == (0, 0, 1)
    assert fake_store.delete_by_paths.call_args.args[1] == ["b.py"]


def test_given_hashes_restrict_the_index_to_an_overlay(indexed_workspace) -> None:
    from sprint_crew.vector.chunker import file_hashes
    from sprint_crew.vector.indexer import index_workspace

    workspace, _fake_store, embedded = indexed_workspace
    subset = {"a.py": file_hashes(workspace)["a.py"]}

    result = index_workspace(workspace, "overlay", repo_key="k", hashes=subset)

    assert result.added == 1
    assert _embedded_paths(embedded) == {"a.py"}
