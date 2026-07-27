"""Non-blocking start, queue visibility, real cancel, restart sweep (roadmap M5).

These are the M5 behavioural assertions from the API side. The "returns before the work
finishes" test asserts on an ``asyncio.Event`` the fake planning never sets, not on a
wall-clock budget — the property that matters is *did not wait*, and a millisecond threshold
would only add CI flake.
"""

from __future__ import annotations

import asyncio

import pytest
from httpx import AsyncClient

from sprint_crew.config import get_settings
from sprint_crew.orchestrator.backlog import BacklogRunStore, backlog_store
from sprint_crew.orchestrator.console_store import console_store
from sprint_crew.orchestrator.event_log import event_log
from sprint_crew.orchestrator.run_recovery import INTERRUPTED_ERROR, sweep_interrupted_runs
from sprint_crew.orchestrator.run_registry import reset_run_registry, run_registry
from sprint_crew.orchestrator.session import SessionStore
from sprint_crew.schemas.console import (
    ConsoleMode,
    ConsoleSession,
    ConsoleSessionStatus,
    SprintRunRef,
)
from sprint_crew.schemas.session import (
    BacklogRun,
    BacklogRunStatus,
    SessionStatus,
    SprintSession,
)


@pytest.fixture(autouse=True)
def isolated_registry():
    """Layered on the shared fresh_stores fixture: the run registry is process-wide, so a
    leaked entry from one test changes admission for the next."""
    reset_run_registry()
    yield
    reset_run_registry()


async def _ready_confirmed(client: AsyncClient) -> str:
    created = await client.post(
        "/v1/console/sessions", json={"mode": "code", "initial_prompt": "Add a hello() helper"}
    )
    session = created.json()
    sid = session["session_id"]
    answers = [
        {
            "question_id": q["question_id"],
            "selected_suggestion_id": q["suggestions"][0]["suggestion_id"],
            "custom_text": None,
        }
        for q in session["clarify_questions"]
    ]
    await client.post(f"/v1/console/sessions/{sid}/clarify", json={"answers": answers})
    await client.post(f"/v1/console/sessions/{sid}/confirm")
    return sid


# --- non-blocking start -------------------------------------------------------


@pytest.mark.asyncio
async def test_start_returns_before_planning_completes(
    api_client: AsyncClient, monkeypatch
) -> None:
    """The old handler awaited clone + index + lane load + ScrumMaster inline."""
    release = asyncio.Event()
    planning_entered = asyncio.Event()

    async def _never_finishes(**_kwargs):
        planning_entered.set()
        await release.wait()

    monkeypatch.setattr("sprint_crew.api.app.run_from_prompt", _never_finishes)
    sid = await _ready_confirmed(api_client)

    resp = await api_client.post(f"/v1/console/sessions/{sid}/start")

    assert resp.status_code == 202
    body = resp.json()
    assert body["status"] == "running"
    assert body["sprint_ref"]["backlog_run_id"]
    # The response is already back while the run body is still parked.
    assert not release.is_set()
    await asyncio.wait_for(planning_entered.wait(), timeout=1)
    release.set()


@pytest.mark.asyncio
async def test_second_start_is_queued_with_a_position(api_client: AsyncClient, monkeypatch) -> None:
    release = asyncio.Event()

    async def _blocks(**_kwargs):
        await release.wait()

    monkeypatch.setattr("sprint_crew.api.app.run_from_prompt", _blocks)

    first = await _ready_confirmed(api_client)
    second = await _ready_confirmed(api_client)
    body_one = (await api_client.post(f"/v1/console/sessions/{first}/start")).json()
    body_two = (await api_client.post(f"/v1/console/sessions/{second}/start")).json()

    assert body_one["status"] == "running"
    assert body_one["queue_position"] is None
    assert body_two["status"] == "queued"
    assert body_two["queue_position"] == 1

    release.set()


@pytest.mark.asyncio
async def test_a_queued_session_flips_to_running_once_admitted(
    api_client: AsyncClient, monkeypatch
) -> None:
    first_release = asyncio.Event()
    second_started = asyncio.Event()

    async def _bodies(**kwargs):
        if kwargs["run_id"] == _first_run_id[0]:
            await first_release.wait()
        else:
            second_started.set()
            await asyncio.sleep(3600)

    _first_run_id: list[str] = []
    monkeypatch.setattr("sprint_crew.api.app.run_from_prompt", _bodies)

    first = await _ready_confirmed(api_client)
    second = await _ready_confirmed(api_client)
    body_one = (await api_client.post(f"/v1/console/sessions/{first}/start")).json()
    _first_run_id.append(body_one["sprint_ref"]["backlog_run_id"])
    await api_client.post(f"/v1/console/sessions/{second}/start")

    first_release.set()
    await asyncio.wait_for(second_started.wait(), timeout=1)

    body_two = (await api_client.get(f"/v1/console/sessions/{second}")).json()
    assert body_two["status"] == "running"
    assert body_two["queue_position"] is None


@pytest.mark.asyncio
async def test_a_queued_run_emits_its_position_on_the_timeline(
    api_client: AsyncClient, monkeypatch
) -> None:
    release = asyncio.Event()

    async def _blocks(**_kwargs):
        await release.wait()

    monkeypatch.setattr("sprint_crew.api.app.run_from_prompt", _blocks)
    first = await _ready_confirmed(api_client)
    second = await _ready_confirmed(api_client)
    await api_client.post(f"/v1/console/sessions/{first}/start")
    await api_client.post(f"/v1/console/sessions/{second}/start")

    events = (await api_client.get(f"/v1/console/sessions/{second}/events")).json()["events"]
    queued = [e for e in events if e["event_type"] == "run_queued"]
    assert len(queued) == 1
    assert queued[0]["detail"]["queue_position"] == 1

    release.set()


# --- cancel -------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cancelling_a_queued_run_is_terminal_immediately(
    api_client: AsyncClient, monkeypatch
) -> None:
    release = asyncio.Event()
    second_started = asyncio.Event()

    async def _bodies(**_kwargs):
        if release.is_set():
            second_started.set()
        await release.wait()

    monkeypatch.setattr("sprint_crew.api.app.run_from_prompt", _bodies)
    first = await _ready_confirmed(api_client)
    second = await _ready_confirmed(api_client)
    await api_client.post(f"/v1/console/sessions/{first}/start")
    queued = (await api_client.post(f"/v1/console/sessions/{second}/start")).json()
    run_id = queued["sprint_ref"]["backlog_run_id"]

    resp = await api_client.post(f"/v1/console/sessions/{second}/cancel")

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "cancelled"
    assert body["cancel_requested_at"] is not None
    assert body["queue_position"] is None
    # The run row must agree — its body never executes, so nothing else would update it.
    assert backlog_store().load(run_id).status is BacklogRunStatus.CANCELLED

    release.set()
    await asyncio.sleep(0)
    assert not second_started.is_set()


@pytest.mark.asyncio
async def test_cancelling_a_running_run_is_pending_not_instant(
    api_client: AsyncClient, monkeypatch
) -> None:
    """Stop is *accepted*: status stays running until the run actually unwinds."""
    started = asyncio.Event()

    async def _blocks(**_kwargs):
        started.set()
        await asyncio.sleep(3600)

    monkeypatch.setattr("sprint_crew.api.app.run_from_prompt", _blocks)
    sid = await _ready_confirmed(api_client)
    await api_client.post(f"/v1/console/sessions/{sid}/start")
    await asyncio.wait_for(started.wait(), timeout=1)

    resp = await api_client.post(f"/v1/console/sessions/{sid}/cancel")

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "running"
    assert body["cancel_requested_at"] is not None

    events = (await api_client.get(f"/v1/console/sessions/{sid}/events")).json()["events"]
    assert [e["event_type"] for e in events if e["event_type"] == "cancel_requested"] == [
        "cancel_requested"
    ]


@pytest.mark.asyncio
async def test_a_cancelled_run_turns_the_session_terminal(
    api_client: AsyncClient, monkeypatch
) -> None:
    """The console session mirrors the run's CANCELLED status on the next read."""
    started = asyncio.Event()

    async def _cooperates(**kwargs):
        started.set()
        store = backlog_store()
        run = store.load(kwargs["run_id"])
        store.save(run.model_copy(update={"status": BacklogRunStatus.CANCELLED}))

    monkeypatch.setattr("sprint_crew.api.app.run_from_prompt", _cooperates)
    sid = await _ready_confirmed(api_client)
    await api_client.post(f"/v1/console/sessions/{sid}/start")
    await asyncio.wait_for(started.wait(), timeout=1)
    await asyncio.sleep(0)

    body = (await api_client.get(f"/v1/console/sessions/{sid}")).json()
    assert body["status"] == "cancelled"


@pytest.mark.asyncio
async def test_cancel_without_a_run_still_works(api_client: AsyncClient) -> None:
    created = await api_client.post("/v1/console/sessions", json={"mode": "code"})
    sid = created.json()["session_id"]
    resp = await api_client.post(f"/v1/console/sessions/{sid}/cancel")
    assert resp.status_code == 200
    assert resp.json()["status"] == "cancelled"
    assert resp.json()["cancel_requested_at"] is None


# --- restart sweep ------------------------------------------------------------


def test_sweep_marks_interrupted_rows_failed() -> None:
    settings = get_settings()
    runs = BacklogRunStore(settings.session_db)
    runs.save(BacklogRun(run_id="r-running", status=BacklogRunStatus.RUNNING, user_prompt="p"))
    runs.save(BacklogRun(run_id="r-pending", status=BacklogRunStatus.PENDING, user_prompt="p"))
    runs.save(BacklogRun(run_id="r-done", status=BacklogRunStatus.COMPLETED, user_prompt="p"))

    sessions = SessionStore(settings.session_db)
    sessions.save(
        SprintSession(
            session_id="s-running",
            status=SessionStatus.RUNNING,
            ticket_key="DEMO-1",
            workspace_root="/tmp/ws",
        )
    )
    sessions.save(
        SprintSession(
            session_id="s-awaiting",
            status=SessionStatus.AWAITING_HUMAN,
            ticket_key="DEMO-2",
            workspace_root="/tmp/ws",
        )
    )

    console = console_store()
    for sid, status in (
        ("cs-running", ConsoleSessionStatus.RUNNING),
        ("cs-queued", ConsoleSessionStatus.QUEUED),
        ("cs-ready", ConsoleSessionStatus.READY),
    ):
        console.save(
            ConsoleSession(
                session_id=sid,
                mode=ConsoleMode.CODE,
                status=status,
                sprint_ref=SprintRunRef(backlog_run_id="r-running"),
                queue_position=1 if status is ConsoleSessionStatus.QUEUED else None,
            )
        )

    swept = sweep_interrupted_runs()

    assert sorted(swept["backlog_runs"]) == ["r-pending", "r-running"]
    assert swept["sprint_sessions"] == ["s-running"]
    assert sorted(swept["console_sessions"]) == ["cs-queued", "cs-running"]

    assert runs.load("r-running").error == INTERRUPTED_ERROR
    assert runs.load("r-done").status is BacklogRunStatus.COMPLETED
    assert sessions.load("s-awaiting").status is SessionStatus.AWAITING_HUMAN
    assert console.load("cs-queued").status is ConsoleSessionStatus.FAILED
    assert console.load("cs-queued").queue_position is None
    assert console.load("cs-ready").status is ConsoleSessionStatus.READY


def test_sweep_is_idempotent() -> None:
    runs = BacklogRunStore(get_settings().session_db)
    runs.save(BacklogRun(run_id="r1", status=BacklogRunStatus.RUNNING, user_prompt="p"))
    assert sweep_interrupted_runs()["backlog_runs"] == ["r1"]
    assert sweep_interrupted_runs()["backlog_runs"] == []


def test_sweep_preserves_an_existing_error() -> None:
    runs = BacklogRunStore(get_settings().session_db)
    runs.save(
        BacklogRun(
            run_id="r1", status=BacklogRunStatus.RUNNING, user_prompt="p", error="real failure"
        )
    )
    sweep_interrupted_runs()
    assert runs.load("r1").error == "real failure"


# --- registry / timeline wiring ----------------------------------------------


@pytest.mark.asyncio
async def test_a_run_is_registered_and_unregistered(api_client: AsyncClient, monkeypatch) -> None:
    release = asyncio.Event()
    entered = asyncio.Event()
    finished = asyncio.Event()

    async def _blocks(**_kwargs):
        entered.set()
        await release.wait()

    async def _teardown() -> None:
        finished.set()

    monkeypatch.setattr("sprint_crew.api.app.run_from_prompt", _blocks)
    monkeypatch.setattr("sprint_crew.api.app.stop_all_lanes", _teardown)
    sid = await _ready_confirmed(api_client)
    body = (await api_client.post(f"/v1/console/sessions/{sid}/start")).json()
    run_id = body["sprint_ref"]["backlog_run_id"]

    assert run_registry().get(run_id) is not None
    # on_admit flips the row to running once the slot is won.
    await asyncio.wait_for(entered.wait(), timeout=1)
    assert backlog_store().load(run_id).status is BacklogRunStatus.RUNNING

    release.set()
    await asyncio.wait_for(finished.wait(), timeout=1)
    assert run_registry().get(run_id) is None


@pytest.mark.asyncio
async def test_planning_events_reach_the_console_timeline(
    api_client: AsyncClient, monkeypatch
) -> None:
    """Start went non-blocking; the emitter must be installed before the prep work or the
    long wait is merely replaced by a silent one."""
    from sprint_crew.orchestrator import prompt_run

    emitted = asyncio.Event()

    async def _fake_plan(workspace_id, prompt, repo_url):
        from sprint_crew.orchestrator.emitter import emit_live
        from sprint_crew.schemas.session import agent_event

        emit_live(agent_event("orchestrator", "workspace_ready", "Workspace prepared"))
        emitted.set()
        raise RuntimeError("stop after planning")

    monkeypatch.setattr(prompt_run, "_plan", _fake_plan)
    sid = await _ready_confirmed(api_client)
    await api_client.post(f"/v1/console/sessions/{sid}/start")
    await asyncio.wait_for(emitted.wait(), timeout=1)

    events = event_log().read(sid, since=0)
    types = [e.event_type for e in events]
    assert "run_queued" in types
    assert "workspace_ready" in types
    ready = next(e for e in events if e.event_type == "workspace_ready")
    assert ready.phase == "planning"
