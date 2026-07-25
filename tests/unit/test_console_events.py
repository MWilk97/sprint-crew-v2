"""GET /v1/console/sessions/{id}/events — timeline endpoint, paging, backfill (M2)."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from sprint_crew.api import console as console_module
from sprint_crew.api.app import app
from sprint_crew.config import get_settings
from sprint_crew.orchestrator.console_store import console_store
from sprint_crew.orchestrator.event_log import event_log
from sprint_crew.orchestrator.session import session_store
from sprint_crew.schemas.console import (
    ConsoleMode,
    ConsoleSession,
    ConsoleSessionStatus,
    SprintRunRef,
)
from sprint_crew.schemas.session import AgentEvent, SessionStatus, SprintSession


@pytest.fixture(autouse=True)
def fresh_stores(tmp_path, monkeypatch):
    monkeypatch.setenv("SPRINT_SESSION_DB", str(tmp_path / "console.db"))
    monkeypatch.setenv("SPRINT_WORKSPACE_BASE", str(tmp_path / "workspaces"))
    get_settings.cache_clear()
    console_module.reset_console_store()
    yield
    console_module.reset_console_store()
    get_settings.cache_clear()


@pytest.fixture
def client(auth_headers: dict[str, str]) -> TestClient:
    return TestClient(app, headers=auth_headers)


def _save_console_session(
    session_id: str = "cs-1",
    *,
    status: ConsoleSessionStatus = ConsoleSessionStatus.RUNNING,
    sprint_session_ids: list[str] | None = None,
) -> ConsoleSession:
    session = ConsoleSession(
        session_id=session_id,
        mode=ConsoleMode.CODE,
        status=status,
        sprint_ref=SprintRunRef(sprint_session_ids=sprint_session_ids or []),
    )
    console_store().save(session)
    return session


def _ev(name: str) -> AgentEvent:
    return AgentEvent(agent="node", event_type=name, summary=name)


def test_events_endpoint_returns_monotonic_seq(client: TestClient) -> None:
    _save_console_session("cs-1")
    event_log().append_many("cs-1", "sp-1", [_ev("a"), _ev("b"), _ev("c")])

    resp = client.get("/v1/console/sessions/cs-1/events?since=0")
    assert resp.status_code == 200
    body = resp.json()
    seqs = [e["seq"] for e in body["events"]]
    assert seqs == [1, 2, 3]
    assert body["next_seq"] == 3
    assert body["complete"] is False  # RUNNING is not terminal


def test_events_paging_drains_each_event_once(client: TestClient) -> None:
    _save_console_session("cs-1")
    event_log().append_many("cs-1", "sp-1", [_ev(f"e{i}") for i in range(5)])

    seen: list[int] = []
    since = 0
    for _ in range(10):  # generous bound; must terminate well before
        body = client.get(f"/v1/console/sessions/cs-1/events?since={since}&limit=2").json()
        if not body["events"]:
            break
        seen.extend(e["seq"] for e in body["events"])
        since = body["next_seq"]

    assert seen == [1, 2, 3, 4, 5]


def test_complete_true_when_session_terminal(client: TestClient) -> None:
    _save_console_session("cs-1", status=ConsoleSessionStatus.COMPLETED)
    event_log().append_many("cs-1", "sp-1", [_ev("a")])

    body = client.get("/v1/console/sessions/cs-1/events?since=0").json()
    assert body["complete"] is True


def test_unknown_session_is_404(client: TestClient) -> None:
    assert client.get("/v1/console/sessions/cs-missing/events").status_code == 404


def test_backfill_projects_legacy_sprint_events(client: TestClient) -> None:
    """A session that ran before the events table existed has events only inside its
    sprint sessions; the endpoint projects them in on first read."""
    session_store().save(
        SprintSession(
            session_id="sp-1",
            status=SessionStatus.AWAITING_HUMAN,
            ticket_key="DEMO-1",
            workspace_root="/tmp/ws",
            events=[_ev("session_started"), _ev("shipped")],
        )
    )
    _save_console_session(
        "cs-1", status=ConsoleSessionStatus.COMPLETED, sprint_session_ids=["sp-1"]
    )
    assert not event_log().has_events("cs-1")  # nothing logged live

    body = client.get("/v1/console/sessions/cs-1/events?since=0").json()
    assert [e["event_type"] for e in body["events"]] == ["session_started", "shipped"]
    assert [e["seq"] for e in body["events"]] == [1, 2]
    # Backfill is persistent: a second read serves from the table, not re-projected.
    assert event_log().has_events("cs-1")
    again = client.get("/v1/console/sessions/cs-1/events?since=0").json()
    assert [e["seq"] for e in again["events"]] == [1, 2]
