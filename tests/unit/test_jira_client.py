from __future__ import annotations

import pytest

from sprint_crew.config import get_settings
from sprint_crew.integrations.jira_client import (
    get_github_client,
    get_jira_client,
    parse_acceptance_criteria_from_description,
    redact_secrets,
)
from sprint_crew.integrations.mocks import MockGitHubClient, MockJiraClient


def test_get_jira_client_returns_mock_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("USE_MOCK_INTEGRATIONS", "true")
    get_settings.cache_clear()
    assert isinstance(get_jira_client(), MockJiraClient)


def test_get_github_client_returns_mock_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("USE_MOCK_INTEGRATIONS", "true")
    get_settings.cache_clear()
    assert isinstance(get_github_client(), MockGitHubClient)


def test_redact_secrets_helper() -> None:
    text = "Authorization: Bearer ghp_abcdefghijklmnopqrstuvwxyz1234567890"
    assert "ghp_" not in redact_secrets(text)


def test_parse_acceptance_criteria_from_description() -> None:
    description = (
        "Implement hello().\n\nAcceptance criteria:\npytest -q passes\n\nNotes: keep it simple"
    )
    assert parse_acceptance_criteria_from_description(description) == "pytest -q passes"
