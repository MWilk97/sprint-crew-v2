from __future__ import annotations

import pytest

from sprint_crew.config import Settings
from tests.integration_live.conftest import TEST_SUMMARY_PREFIX


@pytest.mark.integration_live
def test_jira_create_and_get_ticket_with_acceptance_criteria(
    live_jira,
    integration_live_env: Settings,
) -> None:
    summary = f"{TEST_SUMMARY_PREFIX} roundtrip AC"
    acceptance = "pytest -q passes\nUser can greet with hello()"
    ticket = live_jira.create_issue(
        project_key=integration_live_env.jira_project_key,
        summary=summary,
        description="Round-trip test for acceptance criteria parsing.",
        acceptance_criteria=acceptance,
    )

    fetched = live_jira.get_ticket(ticket.key)
    assert fetched.summary == summary
    assert "pytest -q passes" in fetched.acceptance_criteria
    assert fetched.key == ticket.key


@pytest.mark.integration_live
def test_jira_get_ticket_after_transition(
    live_jira,
    integration_live_env: Settings,
) -> None:
    ticket = live_jira.create_issue(
        project_key=integration_live_env.jira_project_key,
        summary=f"{TEST_SUMMARY_PREFIX} transition roundtrip",
        description="Verify ticket remains readable after workflow transition.",
    )
    live_jira.transition(ticket.key, integration_live_env.jira_review_transition)
    fetched = live_jira.get_ticket(ticket.key)
    assert fetched.key == ticket.key
    assert fetched.summary.endswith("transition roundtrip")


@pytest.mark.integration_live
def test_jira_acceptance_criteria_custom_field(
    live_jira,
    integration_live_env: Settings,
) -> None:
    if not integration_live_env.jira_ac_field:
        pytest.skip("JIRA_AC_FIELD not configured")

    acceptance = "pytest -q passes via custom field"
    ticket = live_jira.create_issue(
        project_key=integration_live_env.jira_project_key,
        summary=f"{TEST_SUMMARY_PREFIX} custom AC field",
        description="Description without AC section — AC only in custom field.",
        acceptance_criteria=acceptance,
    )
    fetched = live_jira.get_ticket(ticket.key)
    assert acceptance in fetched.acceptance_criteria
