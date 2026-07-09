from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest

from sprint_crew.config import Settings
from sprint_crew.integrations.jira_client import get_jira_client
from sprint_crew.orchestrator.sprint import ship
from sprint_crew.schemas.change import CodeChange
from sprint_crew.schemas.session import SessionStatus, SprintSession
from sprint_crew.schemas.ticket import JiraTicket
from tests.integration_live.conftest import TEST_SUMMARY_PREFIX


@pytest.mark.integration_live
@pytest.mark.asyncio
async def test_ship_real_push_and_pr(
    integration_live_env: Settings,
    sandbox_workspace: Path,
    cleanup_ship_artifacts: list[str],
) -> None:
    jira = get_jira_client()
    ticket = jira.create_issue(
        project_key=integration_live_env.jira_project_key,
        summary=f"{TEST_SUMMARY_PREFIX} real ship",
        description="Integration live test for ship() with real GitHub push and PR.",
        acceptance_criteria="pytest -q",
    )

    marker = Path(sandbox_workspace) / ".sprint-crew-integration-marker"
    marker.write_text(f"integration ship test {uuid4().hex[:8]}\n", encoding="utf-8")

    branch = f"feature/{ticket.key.lower()}"
    cleanup_ship_artifacts.append(branch)

    session = SprintSession(
        session_id=str(uuid4()),
        status=SessionStatus.RUNNING,
        ticket_key=ticket.key,
        workspace_root=str(sandbox_workspace),
        selected_ticket=JiraTicket(
            key=ticket.key,
            summary=ticket.summary,
            description=ticket.description,
            status=ticket.status,
            issue_type=ticket.issue_type,
            acceptance_criteria=ticket.acceptance_criteria,
        ),
        code_change=CodeChange(
            ticket_key=ticket.key,
            branch=branch,
            summary="integration test marker file",
            tests_passed=True,
        ),
    )

    shipped = await ship(session)

    assert shipped.status == SessionStatus.AWAITING_HUMAN
    assert shipped.pr_url
    assert integration_live_env.github_repo in shipped.pr_url
    assert shipped.pr_url in str(shipped.events[-1].detail.get("pr_url", shipped.pr_url))

    fetched = jira.get_ticket(ticket.key)
    assert fetched.key == ticket.key
