from __future__ import annotations

import logging

import pytest

from sprint_crew.config import get_settings
from sprint_crew.integrations.jira_client import (
    AtlassianJiraClient,
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


class _FakeAtlassian:
    def __init__(self, transitions: list[dict[str, str]]) -> None:
        self._transitions = transitions
        self.transitioned_to_id: str | None = None

    def get_issue_transitions(self, key: str) -> list[dict[str, str]]:
        return self._transitions

    def set_issue_status_by_transition_id(self, key: str, transition_id: str) -> None:
        self.transitioned_to_id = transition_id


def _jira_client_with_fake(fake: _FakeAtlassian) -> AtlassianJiraClient:
    client = AtlassianJiraClient.__new__(AtlassianJiraClient)
    client._client = fake  # type: ignore[attr-defined]
    return client


def test_transition_applies_matching_transition_case_insensitively() -> None:
    fake = _FakeAtlassian([{"id": "31", "name": "In Review"}])
    client = _jira_client_with_fake(fake)

    client.transition("DEMO-1", "in review")

    assert fake.transitioned_to_id == "31"


def test_transition_logs_error_and_does_not_transition_on_unknown_status(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Regression: a misconfigured JIRA_REVIEW_TRANSITION must not fail silently —
    it should be loud enough in logs to catch during sandbox verification."""
    fake = _FakeAtlassian([{"id": "31", "name": "In Review"}])
    client = _jira_client_with_fake(fake)

    with caplog.at_level(logging.ERROR):
        client.transition("DEMO-1", "Nonexistent Status")

    assert fake.transitioned_to_id is None
    assert "No Jira transition named" in caplog.text
