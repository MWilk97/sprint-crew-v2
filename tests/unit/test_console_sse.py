"""GET /v1/console/sessions/{id}/stream — SSE transport (M3).

Uses httpx.AsyncClient (not TestClient) because Starlette's sync TestClient does not drive
open-ended streaming responses well. A very short SSE_HEARTBEAT_S makes the heartbeat/terminal
check fire fast so a finished run emits ``event: done`` promptly instead of after 15 s.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator

import pytest
from httpx import ASGITransport, AsyncClient
from tests.conftest import TEST_API_TOKEN

from sprint_crew.api import console as console_module
from sprint_crew.api.app import app
from sprint_crew.config import get_settings
from sprint_crew.orchestrator.console_store import console_store
from sprint_crew.orchestrator.event_log import event_log
from sprint_crew.schemas.console import (
    ConsoleMode,
    ConsoleSession,
    ConsoleSessionStatus,
    SprintRunRef,
)
from sprint_crew.schemas.session import AgentEvent


@pytest.fixture(autouse=True)
def fresh_stores(tmp_path, monkeypatch):
    monkeypatch.setenv("SPRINT_SESSION_DB", str(tmp_path / "console.db"))
    monkeypatch.setenv("SPRINT_WORKSPACE_BASE", str(tmp_path / "workspaces"))
    monkeypatch.setenv("SSE_HEARTBEAT_S", "0.05")
    get_settings.cache_clear()
    console_module.reset_console_store()
    yield
    console_module.reset_console_store()
    get_settings.cache_clear()


@pytest.fixture
async def client(auth_headers: dict[str, str]) -> AsyncIterator[AsyncClient]:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test", headers=auth_headers) as ac:
        yield ac


def _ev(name: str) -> AgentEvent:
    return AgentEvent(agent="node", event_type=name, summary=name)


def _save_session(
    session_id: str = "cs-1",
    *,
    status: ConsoleSessionStatus = ConsoleSessionStatus.COMPLETED,
) -> ConsoleSession:
    session = ConsoleSession(
        session_id=session_id,
        mode=ConsoleMode.CODE,
        status=status,
        sprint_ref=SprintRunRef(sprint_session_ids=[]),
    )
    console_store().save(session)
    return session


async def _collect(resp, *, timeout: float = 5.0) -> list[dict]:
    """Parse SSE frames until ``event: done``. Returns a list of {id, event, data, comment}."""

    async def _read() -> list[dict]:
        frames: list[dict] = []
        cur: dict = {}
        async for raw in resp.aiter_lines():
            line = raw.rstrip("\n")
            if line == "":
                if cur:
                    frames.append(cur)
                    if cur.get("event") == "done":
                        break
                    cur = {}
                continue
            if line.startswith(":"):
                cur["comment"] = line[1:].strip()
            elif line.startswith("id:"):
                cur["id"] = line[3:].strip()
            elif line.startswith("event:"):
                cur["event"] = line[6:].strip()
            elif line.startswith("data:"):
                cur["data"] = line[5:].strip()
        return frames

    return await asyncio.wait_for(_read(), timeout=timeout)


def _data_frames(frames: list[dict]) -> list[dict]:
    return [f for f in frames if "data" in f and f.get("event") != "done" and "id" in f]


@pytest.mark.asyncio
async def test_stream_replays_then_signals_done(client: AsyncClient) -> None:
    _save_session("cs-1", status=ConsoleSessionStatus.COMPLETED)
    event_log().append_many("cs-1", "sp-1", [_ev("a"), _ev("b"), _ev("c")])

    async with client.stream("GET", "/v1/console/sessions/cs-1/stream") as resp:
        assert resp.status_code == 200
        frames = await _collect(resp)

    data = _data_frames(frames)
    assert [f["id"] for f in data] == ["1", "2", "3"]
    assert [json.loads(f["data"])["event_type"] for f in data] == ["a", "b", "c"]
    assert frames[-1]["event"] == "done"


@pytest.mark.asyncio
async def test_stream_resume_from_last_event_id_no_gap_no_dup(client: AsyncClient) -> None:
    _save_session("cs-1", status=ConsoleSessionStatus.COMPLETED)
    event_log().append_many("cs-1", "sp-1", [_ev(f"e{i}") for i in range(5)])

    headers = {"Last-Event-ID": "3"}
    async with client.stream("GET", "/v1/console/sessions/cs-1/stream", headers=headers) as resp:
        frames = await _collect(resp)

    data = _data_frames(frames)
    assert [f["id"] for f in data] == ["4", "5"]  # 1-3 already delivered, not re-sent
    assert frames[-1]["event"] == "done"


@pytest.mark.asyncio
async def test_stream_since_query_param_resumes(client: AsyncClient) -> None:
    _save_session("cs-1", status=ConsoleSessionStatus.COMPLETED)
    event_log().append_many("cs-1", "sp-1", [_ev(f"e{i}") for i in range(4)])

    async with client.stream("GET", "/v1/console/sessions/cs-1/stream?since=2") as resp:
        frames = await _collect(resp)

    assert [f["id"] for f in _data_frames(frames)] == ["3", "4"]


@pytest.mark.asyncio
async def test_stream_pushes_live_events(client: AsyncClient) -> None:
    """Events appended after connect arrive over the bus (not just replay); flipping the
    session terminal then drains the stream with event: done.

    The mutation runs as a concurrent task scheduled *before* the stream is entered:
    httpx's in-process ASGITransport drives the app until the response completes, so
    ``stream()`` only returns once the run reaches a terminal state — the flip has to
    happen from another task while the stream is live.
    """
    _save_session("cs-1", status=ConsoleSessionStatus.RUNNING)

    async def _mutate() -> None:
        await asyncio.sleep(0.15)  # let the stream attach and emit at least one heartbeat
        event_log().append_many("cs-1", "sp-1", [_ev("live1"), _ev("live2")])
        _save_session("cs-1", status=ConsoleSessionStatus.COMPLETED)

    task = asyncio.create_task(_mutate())
    try:
        async with client.stream("GET", "/v1/console/sessions/cs-1/stream") as resp:
            frames = await _collect(resp)
    finally:
        await task

    data = _data_frames(frames)
    assert [json.loads(f["data"])["event_type"] for f in data] == ["live1", "live2"]
    assert frames[-1]["event"] == "done"


@pytest.mark.asyncio
async def test_stream_unknown_session_is_404(client: AsyncClient) -> None:
    resp = await client.get("/v1/console/sessions/cs-missing/stream")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_stream_accepts_token_query_param() -> None:
    """Browser EventSource cannot set an Authorization header, so ?token= is accepted."""
    _save_session("cs-1", status=ConsoleSessionStatus.COMPLETED)
    event_log().append_many("cs-1", "sp-1", [_ev("a")])

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:  # no header
        url = f"/v1/console/sessions/cs-1/stream?token={TEST_API_TOKEN}"
        async with ac.stream("GET", url) as resp:
            assert resp.status_code == 200
            frames = await _collect(resp)
    assert [f["id"] for f in _data_frames(frames)] == ["1"]


@pytest.mark.asyncio
async def test_stream_rejects_wrong_token() -> None:
    _save_session("cs-1", status=ConsoleSessionStatus.COMPLETED)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        resp = await ac.get("/v1/console/sessions/cs-1/stream?token=wrong")
        assert resp.status_code == 401
