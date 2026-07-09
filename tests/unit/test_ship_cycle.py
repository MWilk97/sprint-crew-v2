from __future__ import annotations

import subprocess

import pytest

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
