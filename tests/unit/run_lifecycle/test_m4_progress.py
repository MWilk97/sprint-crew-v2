"""Fine-grained, honestly-timestamped progress (roadmap M4).

Covers the live emit contract: tool calls reach the timeline table and SSE bus the moment
they return (not in a node-end batch), carry a call-time timestamp and ``duration_ms``, have
oversized args capped, and are not double-appended by the node-end persistence path. Plus lane
lifecycle events emitted before the multi-minute load block.

The milestone's headline claim — events arrive *while a node is still running*, spread across
the window rather than batched at node end — is asserted end-to-end over SSE with arrival
timings in ``test_tool_events_reach_sse_subscriber_while_node_runs``.
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from starlette.requests import Request

from sprint_crew.agents.tool_events import tool_call_event, tool_call_events
from sprint_crew.api import console as console_module
from sprint_crew.api.console.events import stream_console_events
from sprint_crew.config import Role, get_settings
from sprint_crew.graph import lanes
from sprint_crew.graph.pipeline import _phased
from sprint_crew.orchestrator.console_store import console_store
from sprint_crew.orchestrator.emitter import Emitter, current_emitter, reset_emitter, set_emitter
from sprint_crew.orchestrator.event_log import event_log
from sprint_crew.orchestrator.session import create_and_run_cycle
from sprint_crew.schemas.console import (
    ConsoleMode,
    ConsoleSession,
    ConsoleSessionStatus,
    SprintRunRef,
)
from sprint_crew.schemas.session import AgentEvent, SessionStatus
from sprint_crew.schemas.ticket import JiraTicket, PlanStep, TaskPlan
from sprint_crew.tools.pydantic_ai import _MAX_ARG_CHARS, _record_tool_call, workspace_deps


@pytest.fixture(autouse=True)
def _tmp_db(tmp_path, monkeypatch):
    monkeypatch.setenv("SPRINT_SESSION_DB", str(tmp_path / "events.db"))
    monkeypatch.setenv("SPRINT_WORKSPACE_BASE", str(tmp_path / "workspaces"))
    monkeypatch.setenv("SSE_HEARTBEAT_S", "0.05")
    get_settings.cache_clear()
    console_module.reset_console_store()
    yield
    console_module.reset_console_store()
    get_settings.cache_clear()


@pytest.fixture
def live_emitter():
    """Install a context emitter over a tmp EventLog for the duration of a test."""
    emitter = Emitter(log=event_log(), console_session_id="cs-1", sprint_session_id="sp-1")
    token = set_emitter(emitter)
    try:
        yield emitter
    finally:
        reset_emitter(token)


def _deps(tmp_path: Path):
    return workspace_deps(tmp_path, event_agent="coder", tool_call_log=[])


def _two_step_plan() -> TaskPlan:
    return TaskPlan(
        ticket_key="DEMO-1",
        summary="two steps",
        steps=[
            PlanStep(description="create models", files=["models.py"]),
            PlanStep(description="create api", files=["api.py"]),
        ],
        files_to_touch=["models.py", "api.py"],
        acceptance_tests=["pytest -q"],
    )


def test_record_tool_call_emits_live_with_duration(tmp_path, live_emitter) -> None:
    deps = _deps(tmp_path)
    _record_tool_call(
        deps, "read_file", {"path": "greeter.py"}, "def greet", ok=True, duration_ms=12.5
    )

    events = event_log().read("cs-1", since=0)
    assert len(events) == 1
    (event,) = events
    assert event.event_type == "tool_call"
    assert event.agent == "coder"
    assert event.detail is not None
    assert event.detail["tool"] == "read_file"
    assert event.detail["duration_ms"] == 12.5
    assert event.detail["index"] == 1
    assert event.seq == 1
    assert event.timestamp  # stamped at call time by _record_tool_call


def test_record_tool_call_caps_oversized_args(tmp_path, live_emitter) -> None:
    huge = "x" * (_MAX_ARG_CHARS + 500)
    deps = _deps(tmp_path)
    _record_tool_call(deps, "apply_patch", {"patch": huge}, "applied", ok=True, duration_ms=1.0)

    # Capped both in the persisted log entry (batch path) and in the live-emitted event.
    (entry,) = deps.tool_call_log
    assert len(entry["args"]["patch"]) == _MAX_ARG_CHARS + 1  # + the ellipsis char
    (event,) = event_log().read("cs-1", since=0)
    assert event.detail is not None
    assert event.detail["args"]["patch"].endswith("…")


def test_no_emitter_means_no_live_events_but_batch_preserves_timing(tmp_path) -> None:
    """Fallback path: with no emitter set nothing streams, but the tool log still carries
    call-time timestamp and duration_ms so the node-end batch conversion is honest."""
    assert current_emitter() is None
    deps = _deps(tmp_path)
    _record_tool_call(deps, "grep", {"pattern": "TODO"}, "hits", ok=True, duration_ms=7.0)

    assert event_log().read("cs-1", since=0) == []  # nothing emitted live
    (event,) = tool_call_events("coder", deps.tool_call_log)
    assert event.detail is not None
    assert event.detail["duration_ms"] == 7.0
    assert event.timestamp == deps.tool_call_log[0]["timestamp"]
    # No seq marker, so _persist_progress will append it at node end — the fallback path.
    assert event.seq is None


async def test_tool_index_is_continuous_across_coder_loops(tmp_path, live_emitter) -> None:
    """A single graph node runs several Coder loops (one per plan step, plus continuation
    rounds). They share one tool log, so ``index`` keeps counting instead of restarting at 1
    per loop — otherwise the live stream and the node-end batch would number the same calls
    differently and ``(seq, tool, index)`` would not identify a call.
    """
    from sprint_crew.agents import coder

    shared: list[dict] = []

    async def fake_loop(_plan, root, **kwargs):
        log = kwargs.get("tool_call_log")
        deps = workspace_deps(root, event_agent="coder", tool_call_log=log)
        for _ in range(2):
            _record_tool_call(deps, "read_file", {"path": "a.py"}, "ok", ok=True, duration_ms=1.0)
        return "done", log if log is not None else []

    with patch.object(coder, "run_coder_loop", side_effect=fake_loop):
        await coder.run_coder_plan(_two_step_plan(), tmp_path, tool_call_log=shared)

    assert len(shared) == 4  # two loops x two calls, one shared log
    live = event_log().read("cs-1", since=0)
    assert [e.detail["index"] for e in live if e.detail] == [1, 2, 3, 4]
    # The batch conversion over the merged log agrees with what streamed live.
    assert [e.detail["index"] for e in tool_call_events("coder", shared) if e.detail] == [
        1,
        2,
        3,
        4,
    ]


async def test_persist_progress_dedups_live_tool_events(
    sample_ticket: JiraTicket, tmp_path: Path
) -> None:
    """A live-emitting node also returns its tool_call events via state (batch path). The
    node-end persistence must keep them in SprintSession.events but not re-append the same
    tool events to the timeline table — otherwise every tool call lands twice."""

    async def fake_run_sprint_cycle(state, *, on_node_complete=None):
        emitter = current_emitter()
        assert emitter is not None  # console run installs one
        # Node body: two tools stream live as they return.
        tool_events = [
            emitter.emit(tool_call_event("coder", {"tool": "read_file", "ok": True}, 1)),
            emitter.emit(tool_call_event("coder", {"tool": "write_file", "ok": True}, 2)),
        ]
        # Node end: batch conversion (tool events) + a summary event flow back via state.
        summary = AgentEvent(agent="coder", event_type="code_change", summary="done")
        accumulated = [*tool_events, summary]
        if on_node_complete is not None:
            await on_node_complete({**state, "events": accumulated})
        return {**state, "events": accumulated, "status": SessionStatus.AWAITING_HUMAN}

    with patch(
        "sprint_crew.orchestrator.session.run_sprint_cycle",
        side_effect=fake_run_sprint_cycle,
    ):
        await create_and_run_cycle(
            ticket=sample_ticket,
            workspace=tmp_path,
            session_id="sp-1",
            console_session_id="cs-1",
        )

    events = event_log().read("cs-1", since=0)
    types = [e.event_type for e in events]
    assert types.count("tool_call") == 2  # once each, not four times
    assert types.count("code_change") == 1
    assert [e.seq for e in events] == [1, 2, 3]  # contiguous, no gap from a dropped re-append


def _save_console_session(session_id: str, *, status: ConsoleSessionStatus) -> None:
    console_store().save(
        ConsoleSession(
            session_id=session_id,
            mode=ConsoleMode.CODE,
            status=status,
            sprint_ref=SprintRunRef(sprint_session_ids=[]),
        )
    )


def _bare_request() -> Request:
    """Minimal GET scope; the endpoint only reads the ``last-event-id`` header off it."""
    return Request({"type": "http", "method": "GET", "path": "/", "headers": []})


@pytest.mark.asyncio
async def test_tool_events_stream_during_node_not_batched_at_end(tmp_path: Path) -> None:
    """M4's headline done-when: during a node making 10 tool calls, the stream delivers all
    10 *spread across the window* (asserted on arrival timings, not just count), with the
    first landing before the node finishes. Without the live emit path they would arrive as
    one burst at node end — the M2 batch behaviour — and the spread assertion would fail.

    Drives the endpoint's real ``body_iterator`` rather than an httpx client: httpx's
    in-process ASGITransport runs the app to completion before handing back the response
    (see test_console_sse.py), which collapses every arrival to one timestamp and makes
    per-frame timing unmeasurable through it. Iterating the generator exercises the same
    production code path — replay, bus subscribe, per-event yield — with true timing.
    """
    _save_console_session("cs-1", status=ConsoleSessionStatus.RUNNING)
    emitter = Emitter(log=event_log(), console_session_id="cs-1", sprint_session_id="sp-1")
    deps = workspace_deps(tmp_path, event_agent="coder", tool_call_log=[])
    node_finished_at = 0.0

    async def _node() -> None:
        """Stands in for a Coder node: ten tool calls spaced over the run window."""
        nonlocal node_finished_at
        await asyncio.sleep(0.1)  # let the stream attach and finish replay
        token = set_emitter(emitter)
        try:
            for i in range(10):
                _record_tool_call(
                    deps, "read_file", {"path": f"f{i}.py"}, "ok", ok=True, duration_ms=1.0
                )
                await asyncio.sleep(0.03)  # node still running between calls
        finally:
            reset_emitter(token)
        node_finished_at = time.monotonic()
        _save_console_session("cs-1", status=ConsoleSessionStatus.COMPLETED)

    response = await stream_console_events("cs-1", _bare_request(), since=0)
    task = asyncio.create_task(_node())

    async def _drain() -> list[tuple[float, dict]]:
        out: list[tuple[float, dict]] = []
        async for frame in response.body_iterator:
            if getattr(frame, "event", None) == "done":
                break
            if getattr(frame, "data", None):
                out.append((time.monotonic(), json.loads(frame.data)))
        return out

    try:
        arrivals = await asyncio.wait_for(_drain(), timeout=10.0)
    finally:
        # Breaking out of the generator leaves it suspended at the yield, so its
        # ``finally: bus.unsubscribe(...)`` has not run — the bus is a process-global
        # singleton and would keep this queue attached for every later test.
        await response.body_iterator.aclose()
        await task

    tool_frames = [(at, d) for at, d in arrivals if d["event_type"] == "tool_call"]
    assert len(tool_frames) == 10

    spread = tool_frames[-1][0] - tool_frames[0][0]
    assert spread > 0.1, f"events arrived batched, not streamed (spread {spread:.3f}s)"
    assert tool_frames[0][0] < node_finished_at, "nothing was delivered while the node ran"
    # High-volume events are debug-level so a UI can filter them (M4 volume mitigation).
    assert all(d["level"] == "debug" for _, d in tool_frames)
    assert all(d["detail"]["duration_ms"] is not None for _, d in tool_frames)
    # Continuous, non-repeating index within the node.
    assert [d["detail"]["index"] for _, d in tool_frames] == list(range(1, 11))


@pytest.mark.asyncio
async def test_phased_brackets_a_node_and_binds_phase_to_inner_events(
    tmp_path: Path, live_emitter
) -> None:
    """The phase skeleton: a node is bracketed by phase_started/phase_completed, and events
    emitted inside it inherit the phase so the UI can group them."""

    async def _node(state):
        _record_tool_call(_deps(tmp_path), "read_file", {"path": "a.py"}, "ok", ok=True)
        return {"ok": True}

    result = await _phased("codeImplement", _node)({})
    assert result == {"ok": True}

    events = event_log().read("cs-1", since=0)
    assert [e.event_type for e in events] == ["phase_started", "tool_call", "phase_completed"]
    assert all(e.phase == "codeImplement" for e in events)  # inner event inherited it
    completed = events[-1]
    assert completed.detail is not None
    assert completed.detail["ok"] is True
    assert completed.detail["duration_ms"] is not None


@pytest.mark.asyncio
async def test_phased_reports_failure_when_the_node_raises(live_emitter) -> None:
    """A timeline that says "completed" for a node that raised is misleading exactly when
    it is being read to debug that failure."""

    async def _node(state):
        raise RuntimeError("node blew up")

    with pytest.raises(RuntimeError, match="node blew up"):
        await _phased("review", _node)({})

    completed = event_log().read("cs-1", since=0)[-1]
    assert completed.event_type == "phase_completed"
    assert completed.detail is not None
    assert completed.detail["ok"] is False
    assert completed.level == "error"


@pytest.mark.asyncio
async def test_lane_failure_still_emits_ready_so_a_spinner_can_stop(live_emitter) -> None:
    """A UI keying a spinner on lane_loading→lane_ready would hang forever if a failed load
    emitted nothing. Every exit path reports, with ok=False on failure."""
    proc = MagicMock()
    proc.communicate = AsyncMock(return_value=(b"", b"boom"))
    proc.returncode = 1

    with (
        patch.object(lanes, "stop_lane", new=AsyncMock()),
        patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=proc)),
        patch.object(lanes, "wait_lane_stopped", new=AsyncMock()),
        patch.object(lanes, "_lane_healthy", return_value=False),
    ):
        with pytest.raises(RuntimeError):
            await lanes.ensure_lane(Role.CODING)

    events = event_log().read("cs-1", since=0)
    types = [e.event_type for e in events]
    assert types.index("lane_loading") < types.index("lane_ready")
    ready = next(e for e in events if e.event_type == "lane_ready")
    assert ready.detail is not None
    assert ready.detail["ok"] is False
    assert ready.level == "error"


@pytest.mark.asyncio
async def test_lane_loading_emitted_before_ready(live_emitter) -> None:
    proc = MagicMock()
    proc.communicate = AsyncMock(return_value=(b"", b""))
    proc.returncode = 0

    with (
        patch.object(lanes, "stop_lane", new=AsyncMock()),
        patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=proc)),
        patch.object(lanes, "wait_lane_stopped", new=AsyncMock()),
        # first probe: not healthy (triggers load); second: healthy (emits ready)
        patch.object(lanes, "_lane_healthy", side_effect=[False, True]),
    ):
        await lanes.ensure_lane(Role.CODING)

    events = event_log().read("cs-1", since=0)
    types = [e.event_type for e in events]
    assert types.index("lane_loading") < types.index("lane_ready")
    ready = next(e for e in events if e.event_type == "lane_ready")
    assert ready.detail is not None
    assert ready.detail["duration_ms"] is not None
