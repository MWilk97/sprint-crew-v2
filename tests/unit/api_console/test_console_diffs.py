"""GET /v1/console/sessions/{id}/diff and .../diff/{path} — the review surface (M6)."""

from __future__ import annotations

import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient
from tests.helpers.ticket_fixtures import greeter_code_change, greeter_task_plan, greeter_ticket

from sprint_crew.graph.nodes.review import review
from sprint_crew.orchestrator.console_store import console_store, reap_console_sessions
from sprint_crew.orchestrator.diff_capture import capture_snapshot, record_diff_snapshot
from sprint_crew.orchestrator.diff_store import diff_store
from sprint_crew.orchestrator.emitter import Emitter, reset_emitter, set_emitter
from sprint_crew.orchestrator.event_log import event_log
from sprint_crew.schemas.console import ConsoleMode, ConsoleSession, ConsoleSessionStatus


def _save_console_session(
    session_id: str = "cs-1", *, status: ConsoleSessionStatus = ConsoleSessionStatus.RUNNING
) -> ConsoleSession:
    session = ConsoleSession(session_id=session_id, mode=ConsoleMode.CODE, status=status)
    console_store().save(session)
    return session


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    """A repo with one committed file modified and one new file added."""
    root = tmp_path / "ws"
    root.mkdir()
    args = ["-c", "user.email=t@t", "-c", "user.name=t"]
    subprocess.run(["git", "init", "-q", "."], cwd=root, check=True)
    (root / "keep.py").write_text("a\nb\n", encoding="utf-8")
    subprocess.run(["git", *args, "add", "-A"], cwd=root, check=True)
    subprocess.run(["git", *args, "commit", "-qm", "init"], cwd=root, check=True)
    (root / "keep.py").write_text("a\nB\n", encoding="utf-8")
    (root / "fresh.py").write_text("hello\n", encoding="utf-8")
    return root


def _store_snapshot(
    workspace: Path, *, console_session_id: str = "cs-1", sprint_session_id: str, attempt: int = 0
) -> None:
    snapshot, files = capture_snapshot(
        workspace,
        sprint_session_id=sprint_session_id,
        ticket_key="DEMO-1",
        attempt=attempt,
        console_session_id=console_session_id,
    )
    diff_store().save(snapshot, files)


def test_diff_lists_files_without_hunks(client: TestClient, workspace: Path) -> None:
    """The file-tree payload must stay small: a change of any size is megabytes of hunks and
    a UI renders the tree before it renders one file."""
    _save_console_session()
    _store_snapshot(workspace, sprint_session_id="sp-1")

    body = client.get("/v1/console/sessions/cs-1/diff").json()

    assert body["snapshot"]["sprint_session_id"] == "sp-1"
    assert {f["path"] for f in body["snapshot"]["files"]} == {"keep.py", "fresh.py"}
    assert all("hunks" not in f for f in body["snapshot"]["files"])
    assert body["snapshot"]["total_additions"] == 2


def test_file_diff_carries_hunks(client: TestClient, workspace: Path) -> None:
    _save_console_session()
    _store_snapshot(workspace, sprint_session_id="sp-1")

    body = client.get("/v1/console/sessions/cs-1/diff/keep.py").json()

    assert body["path"] == "keep.py"
    assert body["action"] == "modified"
    kinds = [line["kind"] for line in body["hunks"][0]["lines"]]
    assert kinds == ["context", "del", "add"]


def test_nested_path_survives_the_url(client: TestClient, workspace: Path) -> None:
    """A path parameter with slashes needs the ``:path`` converter; without it every file
    below the repo root would 404."""
    (workspace / "pkg").mkdir()
    (workspace / "pkg" / "mod.py").write_text("inner\n", encoding="utf-8")
    _save_console_session()
    _store_snapshot(workspace, sprint_session_id="sp-1")

    resp = client.get("/v1/console/sessions/cs-1/diff/pkg/mod.py")

    assert resp.status_code == 200
    assert resp.json()["path"] == "pkg/mod.py"


def test_unknown_path_is_404(client: TestClient, workspace: Path) -> None:
    _save_console_session()
    _store_snapshot(workspace, sprint_session_id="sp-1")

    assert client.get("/v1/console/sessions/cs-1/diff/nope.py").status_code == 404


def test_missing_snapshot_is_an_empty_page_not_an_error(client: TestClient) -> None:
    """No diff exists until the first review pass, which is most of a run. A client polling
    this should not have to read 404 as "normal" — 404 means the session is unknown."""
    _save_console_session()

    resp = client.get("/v1/console/sessions/cs-1/diff")

    assert resp.status_code == 200
    assert resp.json() == {"snapshot": None, "available": [], "review": None}
    assert client.get("/v1/console/sessions/cs-missing/diff").status_code == 404


def test_available_lists_every_story_and_attempt(client: TestClient, workspace: Path) -> None:
    """A console session spawns one sprint session per backlog story, each reviewed up to
    MAX_REVIEW_RETRIES times, so "the diff" is never singular. The bare route serves the
    newest capture; the picker needs all of them."""
    _save_console_session()
    _store_snapshot(workspace, sprint_session_id="sp-1", attempt=0)
    _store_snapshot(workspace, sprint_session_id="sp-1", attempt=1)
    _store_snapshot(workspace, sprint_session_id="sp-2", attempt=0)

    body = client.get("/v1/console/sessions/cs-1/diff").json()

    assert [(r["sprint_session_id"], r["attempt"]) for r in body["available"]] == [
        ("sp-1", 0),
        ("sp-1", 1),
        ("sp-2", 0),
    ]
    assert body["snapshot"]["sprint_session_id"] == "sp-2"
    assert all(r["files_changed"] == 2 for r in body["available"])


def test_an_explicit_snapshot_can_be_addressed(client: TestClient, workspace: Path) -> None:
    _save_console_session()
    _store_snapshot(workspace, sprint_session_id="sp-1", attempt=0)
    _store_snapshot(workspace, sprint_session_id="sp-2", attempt=0)

    body = client.get("/v1/console/sessions/cs-1/diff?sprint_session_id=sp-1&attempt=0").json()

    assert body["snapshot"]["sprint_session_id"] == "sp-1"


def test_another_sessions_snapshot_is_not_reachable(client: TestClient, workspace: Path) -> None:
    """console_session_id is on the WHERE clause of every read, so a guessed
    sprint_session_id cannot walk out of the session named in the URL."""
    _save_console_session("cs-1")
    _save_console_session("cs-2")
    _store_snapshot(workspace, console_session_id="cs-2", sprint_session_id="sp-2")

    body = client.get("/v1/console/sessions/cs-1/diff?sprint_session_id=sp-2&attempt=0").json()

    assert body == {"snapshot": None, "available": [], "review": None}


async def test_record_publishes_a_diff_updated_event(workspace: Path) -> None:
    """The UI refreshes off the event stream rather than polling the diff endpoint."""
    _save_console_session()
    token = set_emitter(
        Emitter(log=event_log(), console_session_id="cs-1", sprint_session_id="sp-1")
    )
    try:
        snapshot = await record_diff_snapshot(
            workspace, sprint_session_id="sp-1", ticket_key="DEMO-1"
        )
    finally:
        reset_emitter(token)

    assert snapshot is not None
    (event,) = event_log().read("cs-1", since=0)
    assert event.event_type == "diff_updated"
    assert event.detail["files_changed"] == 2
    assert event.detail["additions"] == 2
    assert diff_store().latest("cs-1") is not None


async def test_review_node_captures_the_snapshot(workspace: Path, passing_review) -> None:
    """Pins the wiring, not just the capture: the review node is the last point before ship
    stages the tree, and it is where the attempt number is known. Capture happens *before*
    the Reviewer call, so the diff reaches the user while the review is still running.
    """
    _save_console_session()
    state = {
        "session_id": "sp-1",
        "workspace_root": str(workspace),
        "selected_ticket": greeter_ticket().model_dump(),
        "task_plan": greeter_task_plan().model_dump(),
        "code_change": greeter_code_change().model_dump(),
        "workspace_diff": "diff",
        "attempt": 2,
    }
    token = set_emitter(
        Emitter(log=event_log(), console_session_id="cs-1", sprint_session_id="sp-1")
    )
    try:
        with (
            patch("sprint_crew.graph.lanes.ensure_lane", new=AsyncMock()),
            patch("sprint_crew.graph.lanes.stop_lane", new=AsyncMock()),
            patch(
                "sprint_crew.agents.reviewer.run_reviewer",
                new=AsyncMock(return_value=passing_review),
            ),
        ):
            await review(state)
    finally:
        reset_emitter(token)

    snapshot = diff_store().latest("cs-1")
    assert snapshot is not None
    assert (snapshot.sprint_session_id, snapshot.attempt) == ("sp-1", 2)
    assert {f.path for f in snapshot.files} == {"keep.py", "fresh.py"}


async def test_no_snapshot_without_a_console_run(workspace: Path) -> None:
    """Gated on the emitter, not a flag: a direct /sprint/* run, smoke_cycle, and most unit
    tests should not pay for several git subprocesses and a parse per review pass."""
    assert await record_diff_snapshot(workspace, sprint_session_id="sp-1") is None
    assert diff_store().latest("cs-1") is None


def test_reaper_drops_diff_rows(workspace: Path) -> None:
    """Snapshots are the largest piece of per-session state, so they must not outlive the
    session the TTL reaper deletes. Age is simulated by moving the reaper's clock forward,
    since the store stamps updated_at on every save."""
    _save_console_session(status=ConsoleSessionStatus.COMPLETED)
    _store_snapshot(workspace, sprint_session_id="sp-1")

    reaped = reap_console_sessions(now=datetime.now(tz=UTC) + timedelta(days=30))

    assert reaped == ["cs-1"]
    assert diff_store().latest("cs-1") is None
