from __future__ import annotations

from pathlib import Path

import pytest
from tests.helpers.agent_live_tickets import complex_api_ticket, email_validators_ticket

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


def test_index_workspace_skips_when_git_sha_unchanged(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _enable_vector(monkeypatch)
    import subprocess
    from unittest.mock import MagicMock, patch

    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
    (tmp_path / "main.py").write_text("x = 1\n", encoding="utf-8")
    subprocess.run(["git", "add", "main.py"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "init"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        env={
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@t",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@t",
        },
    )

    fake_store = MagicMock()
    fake_store.get_collection_git_sha.side_effect = [None, "abc123"]
    fake_vector = [[0.1, 0.2]]

    with (
        patch("sprint_crew.vector.indexer.QdrantStore", return_value=fake_store),
        patch("sprint_crew.vector.indexer.embed_texts", return_value=fake_vector),
        patch("sprint_crew.vector.indexer._git_head_sha", return_value="abc123"),
    ):
        from sprint_crew.vector.indexer import index_workspace

        first = index_workspace(tmp_path, "sha-skip-test")
        assert first.chunks == 1

        with patch("sprint_crew.vector.indexer.iter_workspace_chunks") as mock_chunks:
            second = index_workspace(tmp_path, "sha-skip-test")
            mock_chunks.assert_not_called()
        assert second.chunks == -1
        fake_store.delete_collection.assert_called_once()
