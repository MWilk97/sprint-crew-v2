from __future__ import annotations

import subprocess

import pytest

from sprint_crew.orchestrator.git_commit import (
    branch_for_change,
    commit_change_on_branch,
    commit_message_for_change,
)
from sprint_crew.orchestrator.ship_cycle import ship_stub
from sprint_crew.schemas.change import CodeChange
from sprint_crew.schemas.session import SessionStatus


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
