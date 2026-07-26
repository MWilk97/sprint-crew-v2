from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from tests.helpers.batch_cycle_fakes import (
    awaiting_session,
    trivial_plan,
    trivial_ticket,
)

from sprint_crew.orchestrator.batch_cycle import run_backlog_batched
from sprint_crew.schemas.session import BacklogRunStatus, SessionStatus, SprintSession


@pytest.mark.asyncio
async def test_run_backlog_batched_completes_all_stories(tmp_path: Path) -> None:
    plan = trivial_plan("S1", "S2", "S3")
    tickets = {
        "S1": trivial_ticket("DEMO-1"),
        "S2": trivial_ticket("DEMO-2"),
        "S3": trivial_ticket("DEMO-3"),
    }
    workspace_counter = {"n": 0}

    def fake_prepare(_session_id: str, *, repo_url: str | None = None) -> Path:
        workspace_counter["n"] += 1
        ws = tmp_path / f"ws-{workspace_counter['n']}"
        ws.mkdir()
        return ws

    def fake_chained(_parent: Path, session_id: str) -> Path:
        workspace_counter["n"] += 1
        ws = tmp_path / f"ws-{session_id}"
        ws.mkdir()
        return ws

    async def fake_create_and_run_cycle(**kwargs) -> SprintSession:
        ticket = kwargs["ticket"]
        workspace = kwargs["workspace"]
        session_id = kwargs["session_id"]
        assert kwargs.get("backlog_run_id") == "run-1"
        return awaiting_session(session_id, ticket, workspace)

    with (
        patch("sprint_crew.orchestrator.batch_cycle.create_jira_tickets", return_value=tickets),
        patch("sprint_crew.orchestrator.batch_cycle.prepare_workspace", side_effect=fake_prepare),
        patch(
            "sprint_crew.orchestrator.batch_cycle.prepare_chained_workspace",
            side_effect=fake_chained,
        ),
        patch(
            "sprint_crew.orchestrator.batch_cycle.create_and_run_cycle",
            side_effect=fake_create_and_run_cycle,
        ),
        patch("sprint_crew.orchestrator.batch_cycle.stop_all_lanes", new=AsyncMock()),
    ):
        result = await run_backlog_batched(
            run_id="run-1",
            plan=plan,
            user_prompt="add hello",
            use_real_ship=False,
        )

    assert result.status == BacklogRunStatus.COMPLETED
    assert len(result.session_ids) == 3
    assert len(result.completed_session_ids) == 3


@pytest.mark.asyncio
async def test_run_backlog_batched_stops_on_failed_session(tmp_path: Path) -> None:
    plan = trivial_plan("S1", "S2")
    tickets = {
        "S1": trivial_ticket("DEMO-1"),
        "S2": trivial_ticket("DEMO-2"),
    }

    def fake_prepare(_session_id: str, *, repo_url: str | None = None) -> Path:
        ws = tmp_path / _session_id
        ws.mkdir()
        return ws

    call_count = {"n": 0}

    def fake_chained(parent: Path, session_id: str) -> Path:
        call_count["n"] += 1
        ws = tmp_path / session_id
        ws.mkdir()
        return ws

    async def fake_create_and_run_cycle(**kwargs) -> SprintSession:
        ticket = kwargs["ticket"]
        workspace = kwargs["workspace"]
        session_id = kwargs["session_id"]
        if ticket.key == "DEMO-2":
            return SprintSession(
                session_id=session_id,
                status=SessionStatus.FAILED,
                ticket_key=ticket.key,
                workspace_root=str(workspace),
                selected_ticket=ticket,
                error="Tool 'grep' exceeded max retries count of 1",
            )
        return awaiting_session(session_id, ticket, workspace)

    with (
        patch("sprint_crew.orchestrator.batch_cycle.create_jira_tickets", return_value=tickets),
        patch("sprint_crew.orchestrator.batch_cycle.prepare_workspace", side_effect=fake_prepare),
        patch(
            "sprint_crew.orchestrator.batch_cycle.prepare_chained_workspace",
            side_effect=fake_chained,
        ),
        patch(
            "sprint_crew.orchestrator.batch_cycle.create_and_run_cycle",
            side_effect=fake_create_and_run_cycle,
        ),
        patch("sprint_crew.orchestrator.batch_cycle.stop_all_lanes", new=AsyncMock()),
    ):
        result = await run_backlog_batched(
            run_id="run-1",
            plan=plan,
            user_prompt="add hello",
            use_real_ship=False,
        )

    assert result.status == BacklogRunStatus.FAILED
    assert result.failed_ticket_key == "DEMO-2"
    assert "grep" in (result.error or "")
    assert len(result.completed_session_ids) == 1
