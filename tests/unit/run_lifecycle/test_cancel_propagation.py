"""Cancel reaches the graph, the batch loop, and the coder loops — and lands on
``cancelled``, never ``failed`` (roadmap M5).

The status assertions are the point. ``RunCancelled`` is an ordinary ``Exception``, so the
broad handlers in ``session.py`` / ``batch_cycle.py`` would report a user's Stop as a crash;
``asyncio.CancelledError`` is a ``BaseException``, so it would skip those handlers entirely
and leave the row stuck at ``running``. Both paths are pinned here.
"""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from tests.helpers.batch_cycle_fakes import awaiting_session, trivial_plan, trivial_ticket
from tests.helpers.ticket_fixtures import greeter_ticket

from sprint_crew.agents.coder import run_coder_plan
from sprint_crew.config import get_settings
from sprint_crew.graph.pipeline import _phased
from sprint_crew.orchestrator.batch_cycle import run_backlog_batched
from sprint_crew.orchestrator.run_registry import (
    CancelToken,
    RunCancelled,
    reset_cancel_token,
    set_cancel_token,
)
from sprint_crew.orchestrator.session import create_and_run_cycle, get_session
from sprint_crew.schemas.session import BacklogRunStatus, SessionStatus
from sprint_crew.schemas.ticket import PlanStep, TaskPlan


@pytest.fixture
def cancelled_token():
    """A token already requesting cancel, installed for the duration of the test."""
    token = CancelToken()
    token.request("user stopped")
    handle = set_cancel_token(token)
    yield token
    reset_cancel_token(handle)


@pytest.fixture
def session_db(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("SPRINT_SESSION_DB", str(tmp_path / "sessions.db"))
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def git_workspace(tmp_path: Path) -> Path:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    subprocess.run(["git", "init"], cwd=workspace, check=True, capture_output=True)
    return workspace


# --- graph node checkpoint ----------------------------------------------------


@pytest.mark.asyncio
async def test_phased_node_refuses_to_run_when_cancelled(cancelled_token) -> None:
    ran: list[str] = []

    async def _node(state):
        ran.append("node")
        return {}

    with pytest.raises(RunCancelled, match="user stopped"):
        await _phased("codeImplement", _node)({})
    assert ran == []


@pytest.mark.asyncio
async def test_phased_node_runs_normally_without_a_token() -> None:
    async def _node(state):
        return {"status": SessionStatus.RUNNING}

    assert await _phased("codeImplement", _node)({}) == {"status": SessionStatus.RUNNING}


# --- create_and_run_cycle -----------------------------------------------------


@pytest.mark.asyncio
async def test_cooperative_cancel_marks_the_sprint_session_cancelled(
    session_db, git_workspace: Path
) -> None:
    with patch(
        "sprint_crew.orchestrator.session.run_sprint_cycle",
        new=AsyncMock(side_effect=RunCancelled("user stopped")),
    ):
        session = await create_and_run_cycle(
            ticket=greeter_ticket(),
            workspace=git_workspace,
            session_id="cancel-coop",
        )

    assert session.status is SessionStatus.CANCELLED
    assert session.error == "user stopped"
    assert [e.event_type for e in session.events] == ["cancelled"]
    assert session.events[-1].detail == {"reason": "user stopped", "hard": False}


@pytest.mark.asyncio
async def test_hard_cancel_marks_cancelled_then_reraises(session_db, git_workspace: Path) -> None:
    """CancelledError must be persisted *and* re-raised — swallowing it breaks asyncio."""
    with (
        patch(
            "sprint_crew.orchestrator.session.run_sprint_cycle",
            new=AsyncMock(side_effect=asyncio.CancelledError()),
        ),
        pytest.raises(asyncio.CancelledError),
    ):
        await create_and_run_cycle(
            ticket=greeter_ticket(),
            workspace=git_workspace,
            session_id="cancel-hard",
        )

    persisted = get_session("cancel-hard")
    assert persisted is not None
    assert persisted.status is SessionStatus.CANCELLED
    assert persisted.events[-1].detail == {"reason": None, "hard": True}


@pytest.mark.asyncio
async def test_an_ordinary_error_still_fails_not_cancels(session_db, git_workspace: Path) -> None:
    with patch(
        "sprint_crew.orchestrator.session.run_sprint_cycle",
        new=AsyncMock(side_effect=RuntimeError("lane down")),
    ):
        session = await create_and_run_cycle(
            ticket=greeter_ticket(),
            workspace=git_workspace,
            session_id="boom",
        )

    assert session.status is SessionStatus.FAILED
    assert "lane down" in (session.error or "")


# --- batch loop ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_cancel_between_stories_stops_the_run_and_keeps_shipped_work(
    session_db, tmp_path: Path
) -> None:
    plan = trivial_plan("S1", "S2")
    tickets = {"S1": trivial_ticket("DEMO-1"), "S2": trivial_ticket("DEMO-2")}
    token = CancelToken()
    handle = set_cancel_token(token)
    started: list[str] = []

    def fake_prepare(session_id: str, *, repo_url: str | None = None) -> Path:
        ws = tmp_path / session_id
        ws.mkdir()
        return ws

    async def fake_cycle(**kwargs):
        started.append(kwargs["ticket"].key)
        # The first story ships, then the user hits Stop.
        token.request("user stopped")
        return awaiting_session(kwargs["session_id"], kwargs["ticket"], kwargs["workspace"])

    try:
        with (
            patch("sprint_crew.orchestrator.batch_cycle.create_jira_tickets", return_value=tickets),
            patch(
                "sprint_crew.orchestrator.batch_cycle.prepare_workspace", side_effect=fake_prepare
            ),
            patch("sprint_crew.orchestrator.batch_cycle.create_and_run_cycle", new=fake_cycle),
            patch("sprint_crew.orchestrator.batch_cycle.stop_all_lanes", new=AsyncMock()),
            patch("sprint_crew.orchestrator.batch_cycle.delete_index"),
        ):
            run = await run_backlog_batched(run_id="run-cancel", plan=plan, user_prompt="p")
    finally:
        reset_cancel_token(handle)

    assert started == ["DEMO-1"]
    assert run.status is BacklogRunStatus.CANCELLED
    assert run.error == "user stopped"
    assert len(run.session_ids) == 1
    assert run.completed_session_ids == run.session_ids


@pytest.mark.asyncio
async def test_a_cancelled_story_is_not_reported_as_a_failed_run(
    session_db, tmp_path: Path
) -> None:
    """A story cancelled mid-graph comes back CANCELLED, not raising; the run must agree."""
    plan = trivial_plan("S1", "S2")
    tickets = {"S1": trivial_ticket("DEMO-1"), "S2": trivial_ticket("DEMO-2")}

    def fake_prepare(session_id: str, *, repo_url: str | None = None) -> Path:
        ws = tmp_path / session_id
        ws.mkdir()
        return ws

    async def fake_cycle(**kwargs):
        base = awaiting_session(kwargs["session_id"], kwargs["ticket"], kwargs["workspace"])
        return base.model_copy(update={"status": SessionStatus.CANCELLED, "error": "user stopped"})

    with (
        patch("sprint_crew.orchestrator.batch_cycle.create_jira_tickets", return_value=tickets),
        patch("sprint_crew.orchestrator.batch_cycle.prepare_workspace", side_effect=fake_prepare),
        patch("sprint_crew.orchestrator.batch_cycle.create_and_run_cycle", new=fake_cycle),
        patch("sprint_crew.orchestrator.batch_cycle.stop_all_lanes", new=AsyncMock()),
        patch("sprint_crew.orchestrator.batch_cycle.delete_index"),
    ):
        run = await run_backlog_batched(run_id="run-midcancel", plan=plan, user_prompt="p")

    assert run.status is BacklogRunStatus.CANCELLED
    assert run.failed_ticket_key is None
    assert run.completed_session_ids == []


@pytest.mark.asyncio
async def test_cancel_before_the_first_story_still_stops_lanes(session_db) -> None:
    stop_lanes = AsyncMock()
    token = CancelToken()
    token.request("stopped while queued")
    handle = set_cancel_token(token)
    try:
        with (
            patch("sprint_crew.orchestrator.batch_cycle.stop_all_lanes", new=stop_lanes),
            patch("sprint_crew.orchestrator.batch_cycle._cleanup_vector_overlays"),
            patch("sprint_crew.orchestrator.batch_cycle.create_jira_tickets", return_value={}),
        ):
            run = await run_backlog_batched(
                run_id="run-early", plan=trivial_plan("S1", "S2"), user_prompt="p"
            )
    finally:
        reset_cancel_token(handle)

    assert run.status is BacklogRunStatus.CANCELLED
    assert run.session_ids == []
    stop_lanes.assert_awaited_once()


@pytest.mark.asyncio
async def test_batch_run_without_cancel_is_unaffected(session_db, tmp_path: Path) -> None:
    """Guard: the new checkpoints must be inert when no token is installed."""
    plan = trivial_plan("S1", "S2")
    tickets = {"S1": trivial_ticket("DEMO-1"), "S2": trivial_ticket("DEMO-2")}

    def fake_prepare(session_id: str, *, repo_url: str | None = None) -> Path:
        ws = tmp_path / session_id
        ws.mkdir()
        return ws

    async def fake_cycle(**kwargs):
        return awaiting_session(kwargs["session_id"], kwargs["ticket"], kwargs["workspace"])

    with (
        patch("sprint_crew.orchestrator.batch_cycle.create_jira_tickets", return_value=tickets),
        patch("sprint_crew.orchestrator.batch_cycle.prepare_workspace", side_effect=fake_prepare),
        patch(
            "sprint_crew.orchestrator.batch_cycle.prepare_chained_workspace",
            side_effect=lambda parent, sid: fake_prepare(sid),
        ),
        patch("sprint_crew.orchestrator.batch_cycle.create_and_run_cycle", new=fake_cycle),
        patch("sprint_crew.orchestrator.batch_cycle.stop_all_lanes", new=AsyncMock()),
        patch("sprint_crew.orchestrator.batch_cycle.delete_index"),
    ):
        run = await run_backlog_batched(run_id="run-ok", plan=plan, user_prompt="p")

    assert run.status is BacklogRunStatus.COMPLETED
    assert len(run.completed_session_ids) == 2


# --- coder loops --------------------------------------------------------------


@pytest.mark.asyncio
async def test_coder_step_loop_breaks_on_cancel(tmp_path: Path, cancelled_token) -> None:
    plan = TaskPlan(
        ticket_key="DEMO-1",
        summary="s",
        files_to_touch=["a.py", "b.py"],
        steps=[PlanStep(description="step one"), PlanStep(description="step two")],
        acceptance_tests=["pytest -q"],
    )
    loop = AsyncMock(return_value=("out", []))
    with (
        patch("sprint_crew.agents.coder.run_coder_loop", new=loop),
        patch("sprint_crew.agents.coder.get_settings") as settings,
    ):
        settings.return_value.coder_step_mode = True
        settings.return_value.max_coder_turns = 32
        raw, _logs = await run_coder_plan(plan, tmp_path)

    loop.assert_not_awaited()
    assert raw == ""


@pytest.mark.asyncio
async def test_coder_skips_acceptance_tests_when_cancelled(tmp_path: Path, cancelled_token) -> None:
    """A 900 s acceptance child started after Stop is what makes cancel feel broken."""
    from sprint_crew.agents.coder_coverage import run_coder_with_coverage

    plan = TaskPlan(
        ticket_key="DEMO-1",
        summary="s",
        files_to_touch=["a.py"],
        steps=[PlanStep(description="only step")],
        acceptance_tests=["pytest -q"],
    )
    acceptance = AsyncMock(return_value=("", True))
    with (
        patch("sprint_crew.agents.coder.run_coder_plan", new=AsyncMock(return_value=("out", []))),
        patch(
            "sprint_crew.orchestrator.plan_coverage.validate_plan_coverage",
            return_value=type("C", (), {"satisfied": True})(),
        ),
        patch("sprint_crew.orchestrator.acceptance_tests.run_acceptance_tests", new=acceptance),
    ):
        _raw, _log, _cov, output, verified = await run_coder_with_coverage(plan, tmp_path)

    acceptance.assert_not_called()
    assert output == ""
    assert verified is False
