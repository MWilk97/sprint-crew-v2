"""Append-only event backbone: monotonic cursor, durability, closed vocabulary (M2)."""

from __future__ import annotations

import ast
from pathlib import Path
from unittest.mock import patch

import pytest

from sprint_crew.config import get_settings
from sprint_crew.orchestrator.event_log import EventLog, event_log
from sprint_crew.orchestrator.session import create_and_run_cycle
from sprint_crew.schemas.session import AgentEvent, EventType, SessionStatus
from sprint_crew.schemas.ticket import JiraTicket

# From config, not __file__ depth: this module has moved between directories and a
# hardcoded parents[N] silently scans nothing rather than failing loudly.
SRC_ROOT = get_settings().project_root / "src" / "sprint_crew"


@pytest.fixture(autouse=True)
def _tmp_db(tmp_path, monkeypatch):
    monkeypatch.setenv("SPRINT_SESSION_DB", str(tmp_path / "events.db"))
    monkeypatch.setenv("SPRINT_WORKSPACE_BASE", str(tmp_path / "workspaces"))
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _ev(name: str) -> AgentEvent:
    return AgentEvent(agent="node", event_type=name, summary=name)


def test_append_many_allocates_contiguous_monotonic_seq() -> None:
    log = event_log()
    first = log.append_many("cs-1", "sp-1", [_ev("a"), _ev("b")])
    second = log.append_many("cs-1", "sp-2", [_ev("c")])

    assert [e.seq for e in first] == [1, 2]
    assert [e.seq for e in second] == [3]
    assert log.max_seq("cs-1") == 3


def test_seq_is_per_console_session() -> None:
    log = event_log()
    log.append_many("cs-1", None, [_ev("a"), _ev("b")])
    other = log.append_many("cs-2", None, [_ev("x")])

    assert [e.seq for e in other] == [1]
    assert log.max_seq("cs-1") == 2
    assert log.max_seq("cs-2") == 1


def test_read_paging_yields_each_event_exactly_once() -> None:
    log = event_log()
    log.append_many("cs-1", "sp-1", [_ev(f"e{i}") for i in range(5)])

    seen: list[int] = []
    since = 0
    while True:
        page = log.read("cs-1", since=since, limit=2)
        if not page:
            break
        seqs = [e.seq for e in page]
        assert seqs == sorted(seqs)  # strictly increasing within a page
        assert all(s > since for s in seqs)  # never re-sends <= cursor
        seen.extend(seqs)
        since = page[-1].seq

    assert seen == [1, 2, 3, 4, 5]  # each exactly once, no gaps, no duplicates


def test_events_durable_across_instances() -> None:
    log = event_log()
    log.append_many("cs-1", "sp-1", [_ev("a"), _ev("b")])

    fresh = EventLog(get_settings().session_db)  # simulates an API restart
    reloaded = fresh.read("cs-1", since=0)
    assert [e.event_type for e in reloaded] == ["a", "b"]
    assert [e.seq for e in reloaded] == [1, 2]


def test_detail_and_level_round_trip() -> None:
    log = event_log()
    event = AgentEvent(
        agent="coder",
        event_type="tool_call",
        summary="apply_patch",
        detail={"tool": "apply_patch", "ok": True},
        level="warning",
    )
    log.append_many("cs-1", "sp-1", [event])
    (loaded,) = log.read("cs-1", since=0)
    assert loaded.detail == {"tool": "apply_patch", "ok": True}
    assert loaded.level == "warning"


async def test_create_and_run_cycle_mirrors_events_to_log_without_duplicates(
    sample_ticket: JiraTicket, tmp_path: Path
) -> None:
    """on_node_complete hands over the accumulated list each tick; the log must append
    only the per-node delta so seqs stay contiguous and nothing double-appends."""

    async def fake_run_sprint_cycle(state, *, on_node_complete=None):
        accumulated: list[AgentEvent] = []
        for i in range(3):
            accumulated = [*accumulated, _ev(f"step{i}")]
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

    log = event_log()
    events = log.read("cs-1", since=0)
    assert [e.event_type for e in events] == ["step0", "step1", "step2"]
    assert [e.seq for e in events] == [1, 2, 3]


async def test_no_console_id_means_no_log_rows(sample_ticket: JiraTicket, tmp_path: Path) -> None:
    async def fake_run_sprint_cycle(state, *, on_node_complete=None):
        if on_node_complete is not None:
            await on_node_complete({**state, "events": [_ev("step0")]})
        return {**state, "events": [_ev("step0")], "status": SessionStatus.AWAITING_HUMAN}

    with patch(
        "sprint_crew.orchestrator.session.run_sprint_cycle",
        side_effect=fake_run_sprint_cycle,
    ):
        await create_and_run_cycle(ticket=sample_ticket, workspace=tmp_path, session_id="sp-1")

    assert event_log().read("cs-any", since=0) == []


def _emitted_event_types() -> set[str]:
    """AST-scan src/ for every literal ``event_type`` a call site emits.

    Two syntactic forms cover the codebase: the ``event_type="..."`` keyword and the
    positional ``agent_event(agent, "...")`` / ``_event(agent, "...")`` alias. Dynamic
    (non-literal) event types are the emitter's responsibility and are skipped.
    """
    positional = {"agent_event", "_event"}
    found: set[str] = set()
    for path in SRC_ROOT.rglob("*.py"):
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            for kw in node.keywords:
                if kw.arg == "event_type" and isinstance(kw.value, ast.Constant):
                    if isinstance(kw.value.value, str):
                        found.add(kw.value.value)
            func = node.func
            name = func.id if isinstance(func, ast.Name) else None
            if name in positional and len(node.args) >= 2:
                second = node.args[1]
                if isinstance(second, ast.Constant) and isinstance(second.value, str):
                    found.add(second.value)
    return found


def test_all_emitted_event_types_are_in_the_closed_vocabulary() -> None:
    known = {e.value for e in EventType}
    emitted = _emitted_event_types()
    assert emitted, "scanner found no event_type literals — the scan is broken"
    unknown = emitted - known
    assert not unknown, f"event types emitted but missing from EventType: {sorted(unknown)}"
