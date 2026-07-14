from __future__ import annotations

import subprocess
from unittest.mock import MagicMock, patch

import pytest

from sprint_crew.config import get_settings
from sprint_crew.integrations.mocks import MockGitHubClient, MockJiraClient
from sprint_crew.orchestrator.git_commit import (
    branch_for_change,
    commit_change_on_branch,
    commit_message_for_change,
)
from sprint_crew.orchestrator.ship_cycle import ship_stub
from sprint_crew.orchestrator.sprint import ship
from sprint_crew.schemas.change import CodeChange
from sprint_crew.schemas.session import SessionStatus, SprintSession


@pytest.mark.asyncio
async def test_ship_stub_local_commit(tmp_workspace, code_change: CodeChange) -> None:
    (tmp_workspace / "greeter.py").write_text('def hello():\n    return "hello"\n')
    state = {
        "session_id": "stub-session",
        "workspace_root": str(tmp_workspace),
        "code_change": code_change.model_dump(),
    }
    result = await ship_stub(state)  # type: ignore[arg-type]
    assert result["branch"] == code_change.branch
    assert result["status"] == SessionStatus.AWAITING_HUMAN
    log = subprocess.run(
        ["git", "log", "-1", "--oneline"],
        cwd=tmp_workspace,
        capture_output=True,
        text=True,
        check=True,
    )
    assert code_change.ticket_key in log.stdout


def test_branch_for_change_defaults_from_ticket_key() -> None:
    change = CodeChange(
        ticket_key="DEMO-1",
        branch="feature/demo-1",
        summary="hello",
        tests_passed=True,
    )
    assert branch_for_change(change) == "feature/demo-1"
    change_no_branch = change.model_copy(update={"branch": "feature/custom"})
    assert branch_for_change(change_no_branch) == "feature/custom"


def test_commit_change_on_branch(tmp_workspace, code_change: CodeChange) -> None:
    (tmp_workspace / "greeter.py").write_text('def hello():\n    return "hello"\n')
    branch, msg = commit_change_on_branch(workspace=tmp_workspace, change=code_change)
    assert branch == code_change.branch
    assert msg == commit_message_for_change(code_change)
    log = subprocess.run(
        ["git", "log", "-1", "--oneline"],
        cwd=tmp_workspace,
        capture_output=True,
        text=True,
        check=True,
    )
    assert "DEMO-1" in log.stdout


def _shipready_session(tmp_workspace, code_change: CodeChange) -> SprintSession:
    return SprintSession(
        session_id="ship-session",
        status=SessionStatus.RUNNING,
        ticket_key=code_change.ticket_key,
        workspace_root=str(tmp_workspace),
        code_change=code_change,
    )


@pytest.mark.asyncio
async def test_ship_raises_without_code_change(tmp_workspace) -> None:
    session = SprintSession(
        session_id="ship-session",
        status=SessionStatus.RUNNING,
        ticket_key="DEMO-1",
        workspace_root=str(tmp_workspace),
    )
    with pytest.raises(ValueError, match="no code_change"):
        await ship(session)


@pytest.mark.asyncio
async def test_ship_pushes_opens_pr_and_transitions_jira(
    tmp_workspace, code_change: CodeChange
) -> None:
    session = _shipready_session(tmp_workspace, code_change)
    jira = MockJiraClient()
    github = MockGitHubClient()
    push_ok = MagicMock(returncode=0, stdout="", stderr="")
    store = MagicMock()

    with (
        patch(
            "sprint_crew.orchestrator.sprint.commit_change_on_branch",
            return_value=(code_change.branch, "DEMO-1: ship it"),
        ),
        patch("sprint_crew.orchestrator.sprint.git_run", return_value=push_ok) as git_mock,
        patch("sprint_crew.orchestrator.sprint.origin_repo_slug", return_value="owner/repo"),
        patch("sprint_crew.orchestrator.sprint.get_github_client", return_value=github),
        patch("sprint_crew.orchestrator.sprint.get_jira_client", return_value=jira),
        patch("sprint_crew.orchestrator.session.session_store", return_value=store),
    ):
        shipped = await ship(session)

    push_args = git_mock.call_args.args[0]
    assert push_args[:2] == ["push", "-u"]
    assert shipped.status == SessionStatus.AWAITING_HUMAN
    assert shipped.pr_url and shipped.pr_url.startswith("https://github.example/")
    assert shipped.branch == code_change.branch
    assert jira.comments["DEMO-1"] == [f"PR opened: {shipped.pr_url}"]
    assert jira.transitions["DEMO-1"] == [get_settings().jira_review_transition]
    assert github.pull_requests[0]["head"] == code_change.branch
    store.save.assert_called_once()


@pytest.mark.asyncio
async def test_ship_raises_when_push_fails(tmp_workspace, code_change: CodeChange) -> None:
    session = _shipready_session(tmp_workspace, code_change)
    push_fail = MagicMock(returncode=1, stdout="", stderr="fatal: auth failed api_token=SECRET")

    with (
        patch(
            "sprint_crew.orchestrator.sprint.commit_change_on_branch",
            return_value=(code_change.branch, "DEMO-1: ship it"),
        ),
        patch("sprint_crew.orchestrator.sprint.git_run", return_value=push_fail),
        pytest.raises(RuntimeError) as excinfo,
    ):
        await ship(session)

    assert "SECRET" not in str(excinfo.value)
    assert "[REDACTED]" in str(excinfo.value)
