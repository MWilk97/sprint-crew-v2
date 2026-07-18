from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from sprint_crew.orchestrator.session import _session_from_state, create_and_run_cycle
from sprint_crew.schemas.session import AgentEvent, SessionStatus, SprintSession
from sprint_crew.schemas.ticket import JiraTicket


def _make_base(initial_events: list[AgentEvent]) -> SprintSession:
    return SprintSession(
        session_id="s1",
        status=SessionStatus.RUNNING,
        ticket_key="DEMO-1",
        workspace_root="/tmp/workspace",
        events=initial_events,
    )


def test_session_from_state_preserves_initial_events_across_repeated_calls() -> None:
    """Regression test: a node emitting events must not drop pre-run initial_events
    (e.g. a vector_indexed event set before the graph run starts)."""
    initial_events = [AgentEvent(agent="orchestrator", event_type="vector_indexed", summary="indexed")]
    base = _make_base(initial_events)

    run_events: list[AgentEvent] = []
    for i in range(3):
        run_events = [*run_events, AgentEvent(agent="node", event_type=f"step{i}", summary=f"step {i}")]
        base = _session_from_state({"events": run_events}, base, initial_events=initial_events)
        assert initial_events[0] in base.events

    assert base.events == initial_events + run_events


def test_session_from_state_never_duplicates_events() -> None:
    initial_events: list[AgentEvent] = []
    base = _make_base(initial_events)

    run_events: list[AgentEvent] = []
    for i in range(4):
        run_events = [*run_events, AgentEvent(agent="node", event_type=f"step{i}", summary=f"step {i}")]
        base = _session_from_state({"events": run_events}, base, initial_events=initial_events)

    assert len(base.events) == len(run_events)


async def test_create_and_run_cycle_keeps_initial_event_through_multiple_persist_calls(
    sample_ticket: JiraTicket, tmp_path: Path
) -> None:
    """End-to-end: a multi-node run (simulated via on_node_complete callbacks) must retain
    the initial vector_indexed event in the persisted session after every node completes."""
    initial_event = AgentEvent(agent="orchestrator", event_type="vector_indexed", summary="indexed")

    async def fake_run_sprint_cycle(state, *, on_node_complete=None):
        accumulated_events: list[AgentEvent] = []
        for i in range(3):
            accumulated_events = [
                *accumulated_events,
                AgentEvent(agent="node", event_type=f"step{i}", summary=f"step {i}"),
            ]
            partial = {**state, "events": accumulated_events}
            if on_node_complete is not None:
                await on_node_complete(partial)
        return {**state, "events": accumulated_events, "status": SessionStatus.AWAITING_HUMAN}

    with patch(
        "sprint_crew.orchestrator.session.run_sprint_cycle",
        side_effect=fake_run_sprint_cycle,
    ):
        session = await create_and_run_cycle(
            ticket=sample_ticket,
            workspace=tmp_path,
            initial_events=[initial_event],
        )

    assert initial_event in session.events
    assert len(session.events) == 1 + 3
