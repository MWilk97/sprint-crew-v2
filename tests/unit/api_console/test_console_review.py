"""The human review gate: park, decide, retry, and the ways it ends (roadmap M7).

Snapshots are written straight into the store rather than captured from a real tree —
capture is M6's concern and tested there; what matters here is that a run stops for a
human, and what happens to it afterwards.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient
from httpx import AsyncClient

from sprint_crew.agents.prompts_coder import build_coder_user_prompt
from sprint_crew.config import get_settings
from sprint_crew.graph.nodes.flow import failed, route_after_diff_review, route_after_gate
from sprint_crew.graph.nodes.retry import prepare_rejection_retry
from sprint_crew.graph.nodes.review import await_diff_review
from sprint_crew.orchestrator.backlog import backlog_store
from sprint_crew.orchestrator.console_store import console_store
from sprint_crew.orchestrator.diff_store import DiffStore, diff_store
from sprint_crew.orchestrator.emitter import Emitter, reset_emitter, set_emitter
from sprint_crew.orchestrator.event_log import event_log
from sprint_crew.orchestrator.review_gate import review_gate
from sprint_crew.orchestrator.run_registry import (
    CancelToken,
    RunCancelled,
    reset_cancel_token,
    set_cancel_token,
)
from sprint_crew.schemas.change import ReviewOutcome
from sprint_crew.schemas.console import (
    ConsoleMode,
    ConsoleSession,
    ConsoleSessionStatus,
    SprintRunRef,
)
from sprint_crew.schemas.diff import FileDiff, WorkspaceDiffSnapshot
from sprint_crew.schemas.session import BacklogRun, BacklogRunStatus, utc_now_iso

PATHS = ("keep.py", "fresh.py")


@pytest.fixture(autouse=True)
def quiet_lanes():
    """stop_all_lanes fans out to stop_lane, which would shell out to lane-ctl."""
    with patch("sprint_crew.graph.lanes.stop_lane", new=AsyncMock()) as stop_lane:
        yield stop_lane


@pytest.fixture
async def parked() -> AsyncIterator[asyncio.Event]:
    """Set once the parked run is *answerable*, so tests wait on a signal rather than a tick.

    Fires when the review row is committed, not when the waiter is registered one step
    earlier: the node deliberately registers first (closing the window where a decision
    lands before it can hear one), so the earlier signal races the row into existence and
    a decision posted on it gets a 409.
    """
    signal = asyncio.Event()
    loop = asyncio.get_running_loop()
    real_open_review = DiffStore.open_review

    def _signalling_open_review(self: DiffStore, *args, **kwargs) -> None:
        real_open_review(self, *args, **kwargs)
        # Runs on a to_thread worker; Event.set is not thread-safe.
        loop.call_soon_threadsafe(signal.set)

    with patch.object(DiffStore, "open_review", _signalling_open_review):
        yield signal


def _console_session(
    session_id: str = "cs-1",
    *,
    status: ConsoleSessionStatus = ConsoleSessionStatus.RUNNING,
    sprint_ref: SprintRunRef | None = None,
) -> ConsoleSession:
    session = ConsoleSession(
        session_id=session_id, mode=ConsoleMode.CODE, status=status, sprint_ref=sprint_ref
    )
    console_store().save(session)
    return session


def _store_snapshot(
    *, console: str = "cs-1", sprint_session_id: str = "sp-1", attempt: int = 0, paths=PATHS
) -> None:
    files = [FileDiff(path=path, action="modified", additions=1) for path in paths]
    diff_store().save(
        WorkspaceDiffSnapshot(
            console_session_id=console,
            sprint_session_id=sprint_session_id,
            ticket_key="DEMO-1",
            attempt=attempt,
            files=[f.summary() for f in files],
            total_additions=len(files),
        ),
        files,
    )


def _open_review(
    *, console: str = "cs-1", sprint_session_id: str = "sp-1", attempt: int = 0, rounds: int = 0
) -> None:
    now = datetime.now(tz=UTC)
    diff_store().open_review(
        console,
        sprint_session_id,
        attempt,
        rejection_round=rounds,
        requested_at=now.isoformat(),
        expires_at=(now + timedelta(hours=1)).isoformat(),
    )


def _state(*, attempt: int = 0, rounds: int = 0, session_id: str = "sp-1") -> dict:
    return {
        "session_id": session_id,
        "attempt": attempt,
        "user_rejection_rounds": rounds,
        "tests_run_this_cycle": False,
    }


def _run_gate(state: dict, *, console: str = "cs-1") -> asyncio.Task:
    """Drive the gate node in its own task, with a console emitter installed inside it.

    The emitter is set in the coroutine rather than beside it so the contextvar belongs to
    the task, exactly as ``create_and_run_cycle`` arranges it for a real run.
    """

    async def _body() -> dict:
        token = set_emitter(
            Emitter(
                log=event_log(), console_session_id=console, sprint_session_id=state["session_id"]
            )
        )
        try:
            return await await_diff_review(state)
        finally:
            reset_emitter(token)

    return asyncio.create_task(_body())


def _event_types(console: str = "cs-1") -> list[str]:
    return [e.event_type for e in event_log().read(console, since=0)]


# --- parking and releasing ---------------------------------------------------


async def test_gate_parks_until_the_user_submits(
    api_client: AsyncClient, parked: asyncio.Event, quiet_lanes: AsyncMock
) -> None:
    """The whole point of M7: a change the machine accepted does not ship until a human
    says so. While parked the run is alive, so the lanes must not be."""
    _console_session()
    _store_snapshot()
    task = _run_gate(_state())
    await parked.wait()

    body = (await api_client.get("/v1/console/sessions/cs-1/diff")).json()
    assert body["review"]["status"] == "pending"
    assert body["review"]["undecided_paths"] == ["fresh.py", "keep.py"]
    assert not task.done()
    # Idling a 23 GB model for up to DIFF_REVIEW_TIMEOUT_S costs more than the reload the
    # next node pays (AGENTS.md §4.1).
    assert quiet_lanes.await_count > 0

    resp = await api_client.post(
        "/v1/console/sessions/cs-1/diff/decisions",
        json={
            "decisions": [{"path": p, "decision": "accept"} for p in PATHS],
            "submit": True,
        },
    )

    assert resp.status_code == 200
    assert resp.json()["status"] == "decided"
    result = await asyncio.wait_for(task, timeout=5)
    assert [d["decision"] for d in result["review_decisions"]] == ["accept", "accept"]
    assert route_after_diff_review({**_state(), **result}) == "ship"
    assert "awaiting_diff_review" in _event_types()


async def test_decisions_accumulate_until_submit(
    api_client: AsyncClient, parked: asyncio.Event
) -> None:
    """A half-finished review has to survive a page reload, so recording and releasing are
    separate actions. Only submit closes the review."""
    _console_session()
    _store_snapshot()
    task = _run_gate(_state())
    await parked.wait()

    first = await api_client.post(
        "/v1/console/sessions/cs-1/diff/decisions",
        json={"decisions": [{"path": "keep.py", "decision": "accept"}]},
    )

    assert first.json()["status"] == "pending"
    assert first.json()["undecided_paths"] == ["fresh.py"]
    assert not task.done()

    # Submitting without deciding fresh.py accepts it — an explicit user action, which is
    # why undecided_paths is published for the client to show.
    second = await api_client.post(
        "/v1/console/sessions/cs-1/diff/decisions", json={"submit": True}
    )

    assert second.json()["status"] == "decided"
    result = await asyncio.wait_for(task, timeout=5)
    assert [d["path"] for d in result["review_decisions"]] == ["keep.py"]
    assert route_after_diff_review({**_state(), **result}) == "ship"


async def test_a_verdict_can_be_changed_before_submit(
    api_client: AsyncClient, parked: asyncio.Event
) -> None:
    _console_session()
    _store_snapshot()
    task = _run_gate(_state())
    await parked.wait()

    await api_client.post(
        "/v1/console/sessions/cs-1/diff/decisions",
        json={"decisions": [{"path": "keep.py", "decision": "reject", "reason": "no"}]},
    )
    final = await api_client.post(
        "/v1/console/sessions/cs-1/diff/decisions",
        json={"decisions": [{"path": "keep.py", "decision": "accept"}], "submit": True},
    )

    assert [d["decision"] for d in final.json()["decisions"]] == ["accept"]
    result = await asyncio.wait_for(task, timeout=5)
    assert route_after_diff_review({**_state(), **result}) == "ship"


# --- rejection feeds the retry loop ------------------------------------------


async def test_rejection_reaches_the_coder_prompt(
    api_client: AsyncClient, parked: asyncio.Event
) -> None:
    """Reject means feedback, not surgery. The reason the user typed is the whole value of
    the feature, so pin it all the way into the prompt the Coder actually receives."""
    reason = "this belongs in the service layer, not the handler"
    _console_session()
    _store_snapshot()
    state = _state()
    task = _run_gate(state)
    await parked.wait()

    await api_client.post(
        "/v1/console/sessions/cs-1/diff/decisions",
        json={
            "decisions": [
                {"path": "keep.py", "decision": "accept"},
                {"path": "fresh.py", "decision": "reject", "reason": reason},
            ],
            "submit": True,
        },
    )
    result = await asyncio.wait_for(task, timeout=5)
    assert route_after_diff_review({**state, **result}) == "retry"

    retry = await prepare_rejection_retry({**state, **result})

    assert reason in retry["prior_review_feedback"]
    assert reason in build_coder_user_prompt(
        "{}", prior_review_feedback=retry["prior_review_feedback"]
    )
    # The accepted file is named so the next attempt does not undo work the user kept.
    assert "keep.py" in retry["prior_review_feedback"]
    assert retry["retry_scope"] == "code"
    assert (retry["attempt"], retry["user_rejection_rounds"]) == (1, 1)
    # Exactly one retry per rejection: the verdicts are cleared, so the next pass through
    # the gate cannot route on this round's rejection a second time.
    assert retry["review_decisions"] == []
    assert route_after_diff_review({**state, **retry}) == "ship"


async def test_a_planning_reason_sends_the_retry_back_to_the_tech_lead() -> None:
    """ "Out of scope" is not something the Coder can fix by editing the file it was
    pointed at — the same keyword scan the Reviewer's feedback goes through decides."""
    state = _state()
    state["review_decisions"] = [
        {"path": "fresh.py", "decision": "reject", "reason": "this file is out of scope"}
    ]

    retry = await prepare_rejection_retry(state)

    assert retry["retry_scope"] == "plan"
    assert retry["plan_retries"] == 1


def test_rejection_rounds_do_not_consume_review_retries(passing_review: ReviewOutcome) -> None:
    """A picky human must not exhaust the machine's own budget. ``attempt`` still counts
    every pass — it keys the diff snapshot — so the gate discounts instead."""
    rejected = passing_review.model_copy(update={"passed": False}).model_dump()
    spent = {"review_outcome": rejected, "attempt": 5, "user_rejection_rounds": 0}
    discounted = {"review_outcome": rejected, "attempt": 5, "user_rejection_rounds": 2}

    assert route_after_gate(spent) == "failed"
    assert route_after_gate(discounted) == "retry"


# --- the ways a review ends badly --------------------------------------------


async def test_budget_exhaustion_ends_the_run(
    api_client: AsyncClient, parked: asyncio.Event, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ends cleanly rather than looping — and not by shipping a tree the user said no to."""
    monkeypatch.setenv("MAX_USER_REJECTION_ROUNDS", "1")
    get_settings.cache_clear()
    _console_session()
    _store_snapshot()
    state = _state(rounds=1)
    task = _run_gate(state)
    await parked.wait()

    await api_client.post(
        "/v1/console/sessions/cs-1/diff/decisions",
        json={
            "decisions": [{"path": "keep.py", "decision": "reject", "reason": "still wrong"}],
            "submit": True,
        },
    )
    result = await asyncio.wait_for(task, timeout=5)

    assert result["failure_reason"] == "review_budget_exhausted"
    assert route_after_diff_review({**state, **result}) == "failed"
    assert "review_budget_exhausted" in _event_types()
    # The terminal event carries the slug: neither of the human-gate outcomes is a crash,
    # and a client should not have to parse the error prose to tell them apart.
    (event,) = (await failed({**state, **result}))["events"]
    assert event.summary == "User rejection budget exhausted"
    assert event.detail["reason"] == "review_budget_exhausted"


async def test_an_abandoned_review_expires(
    parked: asyncio.Event, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Nobody decides, so the run must not hold the single admission slot forever."""
    monkeypatch.setenv("DIFF_REVIEW_TIMEOUT_S", "0.05")
    get_settings.cache_clear()
    _console_session()
    _store_snapshot()
    state = _state()

    result = await asyncio.wait_for(_run_gate(state), timeout=5)

    assert result["failure_reason"] == "review_timeout"
    assert route_after_diff_review({**state, **result}) == "failed"
    assert diff_store().pending_review("cs-1") is None
    assert diff_store().get_review("cs-1", "sp-1", 0).status == "expired"
    assert "diff_review_expired" in _event_types()


async def test_stop_unwinds_a_parked_run(parked: asyncio.Event) -> None:
    """Cancel is the escape hatch from a review. The park is the only cancel checkpoint
    that runs while the graph is otherwise idle, so it has to read the token itself."""
    _console_session()
    _store_snapshot()
    token = CancelToken()
    cancel_token = set_cancel_token(token)
    try:
        with patch("sprint_crew.graph.nodes.review._CANCEL_TICK_S", 0.01):
            task = _run_gate(_state())
            await parked.wait()
            token.request("cancelled by user")

            with pytest.raises(RunCancelled):
                await asyncio.wait_for(task, timeout=5)
    finally:
        reset_cancel_token(cancel_token)

    # No review left open, or the console would keep reporting a session that is gone.
    assert diff_store().pending_review("cs-1") is None
    assert review_gate().waiting() == 0


# --- when the gate does not apply --------------------------------------------


async def test_no_park_without_a_console_session() -> None:
    """Gated on the emitter like the M6 capture: a direct /sprint/* run, smoke_cycle, or a
    unit test has nobody who could answer, so parking would simply hang the cycle."""
    _store_snapshot()

    assert await await_diff_review(_state()) == {}
    assert diff_store().pending_review("cs-1") is None


async def test_no_park_when_nothing_was_captured() -> None:
    """Capture is best-effort and never fails a cycle, so a missing snapshot means there is
    nothing to review — not that the user declined to."""
    _console_session()

    assert await asyncio.wait_for(_run_gate(_state()), timeout=5) == {}
    assert diff_store().pending_review("cs-1") is None


async def test_the_gate_is_off_when_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CONSOLE_REVIEW_GATE_ENABLED", "false")
    get_settings.cache_clear()
    _console_session()
    _store_snapshot()

    assert await asyncio.wait_for(_run_gate(_state()), timeout=5) == {}
    assert diff_store().pending_review("cs-1") is None


# --- the endpoint's own rules ------------------------------------------------


def test_decisions_need_an_open_review(client: TestClient) -> None:
    """A verdict nobody is waiting for would silently do nothing; a client whose view is
    that stale needs to hear about it."""
    _console_session()
    _store_snapshot()

    resp = client.post(
        "/v1/console/sessions/cs-1/diff/decisions",
        json={"decisions": [{"path": "keep.py", "decision": "accept"}]},
    )

    assert resp.status_code == 409
    assert client.post("/v1/console/sessions/cs-missing/diff/decisions", json={}).status_code == 404


def test_a_rejection_without_a_reason_is_refused(client: TestClient) -> None:
    """The reason is injected verbatim into the next prompt, so an empty one is a
    validation error rather than something the UI is trusted to enforce."""
    _console_session()
    _store_snapshot()
    _open_review()

    resp = client.post(
        "/v1/console/sessions/cs-1/diff/decisions",
        json={"decisions": [{"path": "keep.py", "decision": "reject", "reason": "  "}]},
    )

    assert resp.status_code == 422


def test_a_path_outside_the_snapshot_is_refused(client: TestClient) -> None:
    _console_session()
    _store_snapshot()
    _open_review()

    resp = client.post(
        "/v1/console/sessions/cs-1/diff/decisions",
        json={"decisions": [{"path": "nope.py", "decision": "accept"}]},
    )

    assert resp.status_code == 400
    assert "nope.py" in resp.json()["detail"]


def test_decisions_cannot_address_a_snapshot_that_is_not_parked(client: TestClient) -> None:
    """Each story of a backlog run parks separately, so a client must follow
    review.sprint_session_id rather than assuming the newest snapshot is the open one."""
    _console_session()
    _store_snapshot(sprint_session_id="sp-1")
    _store_snapshot(sprint_session_id="sp-2")
    _open_review(sprint_session_id="sp-2")

    resp = client.post(
        "/v1/console/sessions/cs-1/diff/decisions",
        json={"sprint_session_id": "sp-1", "attempt": 0, "submit": True},
    )

    assert resp.status_code == 409
    assert "sp-2" in resp.json()["detail"]


def test_the_session_reports_awaiting_review(client: TestClient) -> None:
    """Status stays derived from the run rather than owned (AGENTS.md §4.2): the graph
    parks and unparks inside itself, and only the store sees both sides."""
    _console_session(sprint_ref=SprintRunRef(backlog_run_id="run-1"))
    backlog_store().save(
        BacklogRun(run_id="run-1", status=BacklogRunStatus.RUNNING, user_prompt="do it")
    )
    _store_snapshot()
    _open_review()

    parked = client.get("/v1/console/sessions/cs-1").json()

    assert parked["status"] == "awaiting_review"
    # Messages are refused while parked: the way to speak to a parked run is a decision.
    assert client.post(
        "/v1/console/sessions/cs-1/messages", json={"content": "hi"}
    ).status_code == (409)

    diff_store().close_review("cs-1", "sp-1", 0, status="decided", decided_at=utc_now_iso())

    assert client.get("/v1/console/sessions/cs-1").json()["status"] == "running"


def test_cancel_closes_an_open_review(client: TestClient) -> None:
    """A stopped session must not keep advertising a review nobody is waiting on. The node
    closes its own on a cooperative stop; a hard cancel interrupts that cleanup, so the
    route closes it too — idempotent through the store's pending-only guard."""
    _console_session(sprint_ref=SprintRunRef(backlog_run_id="run-1"))
    backlog_store().save(
        BacklogRun(run_id="run-1", status=BacklogRunStatus.RUNNING, user_prompt="do it")
    )
    _store_snapshot()
    _open_review()

    resp = client.post("/v1/console/sessions/cs-1/cancel")

    assert resp.status_code == 200
    assert resp.json()["status"] == "cancelled"
    assert diff_store().pending_review("cs-1") is None
    assert client.get("/v1/console/sessions/cs-1/diff").json()["review"] is None


def test_reaper_drops_review_rows(client: TestClient) -> None:
    """Reviews and decisions must not outlive the session the TTL reaper deletes."""
    _console_session()
    _store_snapshot()
    _open_review()

    diff_store().delete_for_console_session("cs-1")

    assert diff_store().pending_review("cs-1") is None
    assert diff_store().get_review("cs-1", "sp-1", 0) is None
