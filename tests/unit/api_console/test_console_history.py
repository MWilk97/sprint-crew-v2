"""Session history: list and delete (roadmap M8)."""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from sprint_crew.config import get_settings
from sprint_crew.orchestrator.console_store import console_store
from sprint_crew.orchestrator.event_log import event_log
from sprint_crew.schemas.console import ConsoleSessionStatus


def _create(client: TestClient, prompt: str | None = "Add a hello() helper") -> dict:
    resp = client.post("/v1/console/sessions", json={"mode": "code", "initial_prompt": prompt})
    assert resp.status_code == 201
    return resp.json()


def test_list_returns_newest_first_with_titles(client: TestClient) -> None:
    _create(client, "first request")
    _create(client, "second request")

    page = client.get("/v1/console/sessions").json()

    assert page["total"] == 2
    assert page["next_offset"] is None
    assert [s["title"] for s in page["sessions"]] == ["second request", "first request"]
    # Summaries only: a history sidebar renders none of the unbounded fields.
    assert "messages" not in page["sessions"][0]


def test_list_pages(client: TestClient) -> None:
    for i in range(3):
        _create(client, f"request {i}")

    first = client.get("/v1/console/sessions?limit=2").json()
    assert len(first["sessions"]) == 2
    assert first["next_offset"] == 2

    second = client.get("/v1/console/sessions?limit=2&offset=2").json()
    assert len(second["sessions"]) == 1
    assert second["next_offset"] is None


def test_delete_removes_the_session_its_clone_and_its_timeline(client: TestClient) -> None:
    session = _create(client)
    session_id = session["session_id"]
    workspace = get_settings().workspace_base / session_id
    workspace.mkdir(parents=True)
    (workspace / "file.py").write_text("x = 1\n", encoding="utf-8")

    assert client.delete(f"/v1/console/sessions/{session_id}").status_code == 204

    assert console_store().load(session_id) is None
    assert not Path(workspace).exists()
    assert event_log().read(session_id) == []
    assert client.get(f"/v1/console/sessions/{session_id}").status_code == 404


def test_delete_is_refused_while_a_run_is_live(client: TestClient) -> None:
    """Deleting under a running agent would leave it writing into a workspace nobody owns."""
    session = _create(client)
    stored = console_store().load(session["session_id"])
    assert stored is not None
    stored.status = ConsoleSessionStatus.RUNNING
    console_store().save(stored)

    resp = client.delete(f"/v1/console/sessions/{session['session_id']}")

    assert resp.status_code == 409
    assert "cancel it before deleting" in resp.json()["detail"]
    assert console_store().load(session["session_id"]) is not None


def test_delete_unknown_session_is_404(client: TestClient) -> None:
    assert client.delete("/v1/console/sessions/cs-nope").status_code == 404
