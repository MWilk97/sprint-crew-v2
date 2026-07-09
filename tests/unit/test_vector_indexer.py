from __future__ import annotations

from pathlib import Path

import pytest
from tests.helpers.agent_live_tickets import complex_api_ticket, email_validators_ticket

from sprint_crew.schemas.ticket import JiraTicket
from sprint_crew.vector.indexer import should_index_workspace, should_use_vector


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
