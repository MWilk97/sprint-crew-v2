from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest

from sprint_crew.config import Settings
from sprint_crew.integrations.jira_client import get_jira_client
from sprint_crew.orchestrator.session import create_and_run_cycle, prepare_workspace
from sprint_crew.schemas.session import SprintSession
from sprint_crew.schemas.ticket import JiraTicket
from tests.helpers.ac_targets import pytest_target_from_ac
from tests.helpers.cycle_assertions import assert_cycle_passed
from tests.helpers.vllm_live import docker_available, wait_lane_healthy
from tests.integration_live.conftest import (
    TEST_SUMMARY_PREFIX,
    auth_github_repo_url,
    close_pull_requests_for_branch,
    delete_remote_branch,
)


def _fixture_repo_slug(settings: Settings, fixture_rel: Path) -> str:
    greeter = settings.project_root / "fixtures" / "repo"
    resolved = fixture_rel.resolve()
    if resolved == greeter.resolve():
        slug = settings.github_fixture_repo_greeter
        if not slug:
            return ""
        return slug
    raise ValueError(f"Unknown fixture for ship_live: {fixture_rel}")


async def run_fixture_ship_live_cycle(
    settings: Settings,
    *,
    fixture_rel: Path,
    summary_suffix: str,
    description: str,
    acceptance_criteria: str = "pytest -q passes",
    max_wall_seconds: float = 4500,
) -> SprintSession:
    if not docker_available():
        pytest.skip("docker not available")

    repo_slug = _fixture_repo_slug(settings, fixture_rel)
    if not repo_slug:
        pytest.skip(
            "GITHUB_FIXTURE_REPO_GREETER not configured (run scripts/bootstrap_fixture_repos.sh)"
        )

    jira = get_jira_client()
    ticket = jira.create_issue(
        project_key=settings.jira_project_key,
        summary=f"{TEST_SUMMARY_PREFIX} {summary_suffix}",
        description=description,
        acceptance_criteria=acceptance_criteria,
    )
    jira_ticket = JiraTicket(
        key=ticket.key,
        summary=ticket.summary,
        description=ticket.description,
        status=ticket.status,
        issue_type=ticket.issue_type,
        acceptance_criteria=ticket.acceptance_criteria,
    )

    repo_url = auth_github_repo_url(settings, repo_slug)
    workspace = prepare_workspace(
        f"{summary_suffix.replace(' ', '-')}-{uuid4().hex[:8]}",
        repo_url=repo_url,
    )

    branch = f"feature/{ticket.key.lower()}"
    session: SprintSession | None = None
    try:
        wait_lane_healthy(settings.vllm_coder_url.replace("/v1", "/health"))
        session = await create_and_run_cycle(
            ticket=jira_ticket,
            workspace=workspace,
            use_real_ship=True,
            max_wall_seconds=max_wall_seconds,
        )
        assert_cycle_passed(
            session,
            workspace=workspace,
            test_target=pytest_target_from_ac(acceptance_criteria),
        )
        assert session.pr_url
        assert repo_slug in session.pr_url
        return session
    finally:
        cleanup_branch = session.branch if session and session.branch else branch
        close_pull_requests_for_branch(settings, cleanup_branch, repo_slug=repo_slug)
        delete_remote_branch(settings, cleanup_branch, repo_slug=repo_slug)
