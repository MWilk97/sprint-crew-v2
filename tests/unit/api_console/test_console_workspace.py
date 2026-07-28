"""The session-scoped checkout prepared at creation (roadmap M8)."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
from httpx import AsyncClient

from sprint_crew.api.console.state import load_session, touch
from sprint_crew.api.console.workspace import _record, drain_workspace_prep
from sprint_crew.config import get_settings
from sprint_crew.orchestrator.console_store import (
    console_store,
    enforce_workspace_lru,
    sweep_interrupted_workspace_prep,
)
from sprint_crew.schemas.console import (
    ConsoleMessage,
    ConsoleMessageRole,
    ConsoleMode,
    ConsoleSession,
    ConsoleSessionStatus,
    IndexStatus,
    WorkspaceStatus,
)

PREPARE = "sprint_crew.api.console.workspace.prepare_workspace"


@pytest.fixture
def workspace_prep_on(monkeypatch: pytest.MonkeyPatch):
    """Opt in to the background clone the rest of the suite switches off."""
    monkeypatch.setenv("CONSOLE_WORKSPACE_ON_CREATE", "true")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


async def _create(api_client: AsyncClient) -> dict:
    resp = await api_client.post("/v1/console/sessions", json={"mode": "code"})
    assert resp.status_code == 201
    return resp.json()


async def _events(api_client: AsyncClient, session_id: str) -> list[dict]:
    resp = await api_client.get(f"/v1/console/sessions/{session_id}/events?since=0")
    assert resp.status_code == 200
    return resp.json()["events"]


async def test_create_reports_cloning_then_reaches_ready(
    api_client: AsyncClient, workspace_prep_on
) -> None:
    created = await _create(api_client)
    # The 201 already says the clone is under way — the FE renders "preparing repository"
    # without waiting for a second request to tell it so.
    assert created["workspace_status"] == "cloning"
    assert created["workspace_root"] is None

    await drain_workspace_prep()

    session = (await api_client.get(f"/v1/console/sessions/{created['session_id']}")).json()
    assert session["workspace_status"] == "ready"
    assert Path(session["workspace_root"]).is_dir()
    # Indexing is off in unit tests, which is "nothing to do", not a failure.
    assert session["index_status"] == "skipped"

    types = [e["event_type"] for e in await _events(api_client, created["session_id"])]
    assert types[:2] == ["repo_cloning", "repo_ready"]


async def test_a_broken_index_is_reported_as_failed_not_skipped(
    api_client: AsyncClient, workspace_prep_on, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A dead embed sidecar reported as "skipped" would send the user looking in the wrong
    place; only "nothing worth indexing" is a skip."""
    monkeypatch.setenv("VECTOR_INDEX_ENABLED", "true")
    get_settings.cache_clear()

    with patch(
        "sprint_crew.api.console.workspace.maybe_index_shared",
        return_value=None,
    ):
        created = await _create(api_client)
        await drain_workspace_prep()

    session = (await api_client.get(f"/v1/console/sessions/{created['session_id']}")).json()
    assert session["workspace_status"] == "ready"
    assert session["index_status"] == "failed"
    assert session["index_error"]


async def test_a_failed_clone_leaves_the_session_usable(
    api_client: AsyncClient, workspace_prep_on
) -> None:
    with patch(PREPARE, side_effect=RuntimeError("git clone failed: no such host")):
        created = await _create(api_client)
        await drain_workspace_prep()

    session = (await api_client.get(f"/v1/console/sessions/{created['session_id']}")).json()
    assert session["workspace_status"] == "failed"
    assert "no such host" in session["workspace_error"]
    assert session["status"] == "collecting"

    # The session is not the workspace: clarify still runs without a checkout (grounding
    # it on the repo is M9's job, not this milestone's).
    resp = await api_client.post(
        f"/v1/console/sessions/{created['session_id']}/messages",
        json={"content": "Add a hello() helper"},
    )
    assert resp.status_code == 200
    assert resp.json()["status"] in {"clarifying", "ready"}

    types = [e["event_type"] for e in await _events(api_client, created["session_id"])]
    assert "repo_failed" in types


async def test_prep_never_overwrites_a_concurrent_clarify(
    api_client: AsyncClient, workspace_prep_on
) -> None:
    """The prep task holds a ConsoleSession from before clarify ran; if it saved that
    object instead of re-reading under the lock, it would silently erase the questions."""
    created = await _create(api_client)
    await drain_workspace_prep()

    session = await load_session(created["session_id"])
    assert session is not None
    session.messages.append(
        ConsoleMessage(role=ConsoleMessageRole.USER, content="written by another handler")
    )
    await touch(session)

    await _record(created["session_id"], index_status=IndexStatus.READY)

    after = await load_session(created["session_id"])
    assert after is not None
    assert after.index_status is IndexStatus.READY
    assert [m.content for m in after.messages] == ["written by another handler"]


def _stored(session_id: str, **overrides) -> ConsoleSession:
    overrides.setdefault("status", ConsoleSessionStatus.COMPLETED)
    session = ConsoleSession(session_id=session_id, mode=ConsoleMode.CODE, **overrides)
    console_store().save(session)
    return session


def test_restart_fails_a_session_left_mid_clone() -> None:
    """A prep task dies with the process; the row would advertise cloning forever."""
    _stored("cs-cloning", workspace_status=WorkspaceStatus.CLONING)
    _stored(
        "cs-indexing", workspace_status=WorkspaceStatus.READY, index_status=IndexStatus.INDEXING
    )
    _stored("cs-done", workspace_status=WorkspaceStatus.READY, index_status=IndexStatus.READY)

    swept = sweep_interrupted_workspace_prep()

    assert sorted(swept) == ["cs-cloning", "cs-indexing"]
    cloning = console_store().load("cs-cloning")
    assert cloning is not None
    assert cloning.workspace_status is WorkspaceStatus.FAILED
    assert cloning.workspace_error == "interrupted by restart"
    indexing = console_store().load("cs-indexing")
    assert indexing is not None
    assert indexing.index_status is IndexStatus.FAILED
    assert console_store().load("cs-done").index_status is IndexStatus.READY


def test_workspace_lru_evicts_the_coldest_terminal_clones(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CONSOLE_MAX_WORKSPACES", "2")
    get_settings.cache_clear()
    base = get_settings().workspace_base

    for name in ("cs-old", "cs-mid", "cs-new"):
        (base / name).mkdir(parents=True)
        _stored(name, workspace_status=WorkspaceStatus.READY, workspace_root=str(base / name))

    evicted = enforce_workspace_lru()

    assert evicted == ["cs-old"]
    assert not (base / "cs-old").exists()
    assert (base / "cs-new").exists()
    reclaimed = console_store().load("cs-old")
    assert reclaimed is not None
    # Still readable — only its checkout is gone.
    assert reclaimed.workspace_status is WorkspaceStatus.EVICTED
    assert reclaimed.workspace_root is None


def test_workspace_lru_leaves_a_live_session_alone(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CONSOLE_MAX_WORKSPACES", "1")
    get_settings.cache_clear()
    base = get_settings().workspace_base

    for name in ("cs-running", "cs-done"):
        (base / name).mkdir(parents=True)
    _stored(
        "cs-running",
        status=ConsoleSessionStatus.RUNNING,
        workspace_status=WorkspaceStatus.READY,
        workspace_root=str(base / "cs-running"),
    )
    _stored("cs-done", workspace_status=WorkspaceStatus.READY, workspace_root=str(base / "cs-done"))

    assert enforce_workspace_lru() == []
    assert (base / "cs-running").exists()
