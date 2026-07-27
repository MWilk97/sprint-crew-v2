"""RunRegistry: single-slot admission, queue positions, and both cancel paths (M5)."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from types import SimpleNamespace

import pytest

from sprint_crew.orchestrator.run_registry import (
    CancelToken,
    RunCancelled,
    RunEntry,
    RunRegistry,
    check_cancelled,
    current_cancel_token,
)

_TIMEOUT_S = 2.0


async def _until(predicate: Callable[[], bool], what: str, *, timeout: float = _TIMEOUT_S) -> None:
    """Wait for a condition, bounded by a real timeout.

    Replaces a fixed "yield five times" helper. Counting loop iterations couples every test
    in this file to how many await points the implementation happens to have, so adding one
    broke them all at once for a reason unrelated to what they assert.
    """
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError(f"timed out after {timeout}s waiting for {what}")
        await asyncio.sleep(0.005)


async def _finished(entry: RunEntry, *, timeout: float = _TIMEOUT_S) -> None:
    """Wait for a run to unwind completely — body and wrapper both done."""
    await _until(
        lambda: entry.wrapper_task is not None and entry.wrapper_task.done(),
        f"run {entry.run_id} to finish",
        timeout=timeout,
    )


@pytest.fixture
def instant_escalation(monkeypatch):
    """Make the hard-cancel watchdog fire immediately.

    Patches the registry module's `get_settings` rather than writing `cancel_grace_s` onto
    the object `get_settings()` returns — that object is an lru_cached singleton shared with
    every other test in the process, and `raising=False` there silenced the one signal that
    would catch a renamed field.
    """
    monkeypatch.setattr(
        "sprint_crew.orchestrator.run_registry.get_settings",
        lambda: SimpleNamespace(cancel_grace_s=0.0),
    )


# --- CancelToken --------------------------------------------------------------


def test_token_request_is_idempotent() -> None:
    token = CancelToken()
    assert not token.cancelled
    token.request("first")
    first_at = token.requested_at
    token.request("second")
    assert token.reason == "first"
    assert token.requested_at == first_at


def test_token_check_raises_once_requested() -> None:
    token = CancelToken()
    token.check()
    token.request("stop please")
    with pytest.raises(RunCancelled, match="stop please"):
        token.check()


# --- admission ----------------------------------------------------------------


@pytest.mark.asyncio
async def test_only_one_run_executes_at_a_time() -> None:
    registry = RunRegistry()
    order: list[str] = []
    gates = {name: asyncio.Event() for name in ("a", "b")}

    def body(name: str):
        async def _body() -> None:
            order.append(f"{name}:start")
            await gates[name].wait()
            order.append(f"{name}:end")

        return _body

    entry_a = registry.submit("a", body("a"))
    entry_b = registry.submit("b", body("b"))
    await _until(lambda: "a:start" in order, "run a to start")

    # b is parked on the semaphore: it has not started, and reports one run ahead of it.
    assert order == ["a:start"]
    assert registry.position("b") == 1
    assert registry.position("a") is None
    assert registry.active_run_id() == "a"

    gates["a"].set()
    await _finished(entry_a)
    await _until(lambda: "b:start" in order, "run b to be admitted")
    gates["b"].set()
    await _finished(entry_b)

    assert order == ["a:start", "a:end", "b:start", "b:end"]
    assert registry.queue_depth() == 0


@pytest.mark.asyncio
async def test_a_lone_run_reports_position_zero_not_one() -> None:
    """0 means "starting now". Counting raw queue index would make a single run report as
    waiting on nothing and flicker queued→running in the API for one poll."""
    registry = RunRegistry()
    gate = asyncio.Event()

    async def _body() -> None:
        await gate.wait()

    entry = registry.submit("solo", _body)
    # Before its task has even run: still in _waiting, but nothing is ahead.
    assert registry.position("solo") == 0

    await _until(lambda: registry.position("solo") is None, "solo to be admitted")
    gate.set()
    await _finished(entry)


@pytest.mark.asyncio
async def test_body_sees_the_cancel_token_in_context() -> None:
    registry = RunRegistry()
    seen: list[CancelToken | None] = []

    async def _body() -> None:
        seen.append(current_cancel_token())

    entry = registry.submit("r1", _body)
    await _finished(entry)
    assert seen == [entry.token]


# --- cancel: queued -----------------------------------------------------------


@pytest.mark.asyncio
async def test_cancelling_a_queued_run_never_starts_it() -> None:
    registry = RunRegistry()
    started: list[str] = []
    gate = asyncio.Event()

    async def _blocker() -> None:
        started.append("a")
        await gate.wait()

    async def _second() -> None:
        started.append("b")

    entry_a = registry.submit("a", _blocker)
    entry_b = registry.submit("b", _second)
    await _until(lambda: "a" in started, "run a to start")

    assert registry.cancel("b", reason="user stopped") is True
    gate.set()
    await _finished(entry_a)
    await _finished(entry_b)

    assert started == ["a"]
    assert registry.queue_depth() == 0


# --- cancel: running, cooperative --------------------------------------------


@pytest.mark.asyncio
async def test_cooperative_cancel_unwinds_via_run_cancelled() -> None:
    registry = RunRegistry()
    outcome: list[str] = []
    checkpoint = asyncio.Event()

    async def _body() -> None:
        try:
            checkpoint.set()
            while True:
                await asyncio.sleep(0)
                check_cancelled()
        except RunCancelled as exc:
            outcome.append(f"cancelled:{exc}")
            raise

    entry = registry.submit("r1", _body)
    await checkpoint.wait()

    registry.cancel("r1", reason="user stopped")
    await _finished(entry)

    assert outcome == ["cancelled:user stopped"]


@pytest.mark.asyncio
async def test_cooperative_cancel_does_not_hard_cancel_within_the_grace_window() -> None:
    """The watchdog must not fire while the body is still unwinding cooperatively.

    Uses the real CANCEL_GRACE_S (30 s), so "has not fired yet" is what is being observed.
    """
    registry = RunRegistry()
    hard_cancelled: list[bool] = []
    checkpoint = asyncio.Event()

    async def _body() -> None:
        checkpoint.set()
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            hard_cancelled.append(True)
            raise

    entry = registry.submit("r1", _body)
    await checkpoint.wait()
    registry.cancel("r1")
    await _until(lambda: entry.state == "cancelling", "cancel to be recorded")

    assert hard_cancelled == []
    assert entry.state == "cancelling"
    registry.reset()


# --- cancel: running, hard escalation ----------------------------------------


@pytest.mark.asyncio
async def test_hard_escalation_cancels_a_body_that_ignores_the_token(
    instant_escalation,
) -> None:
    """A body blocked in a subprocess never sees the token; the watchdog must stop it."""
    registry = RunRegistry()
    hard_cancelled: list[bool] = []
    checkpoint = asyncio.Event()

    async def _stubborn() -> None:
        checkpoint.set()
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            hard_cancelled.append(True)
            raise

    entry = registry.submit("r1", _stubborn)
    await checkpoint.wait()
    registry.cancel("r1")
    await _finished(entry)

    assert hard_cancelled == [True]


@pytest.mark.asyncio
async def test_hard_cancel_still_runs_teardown(instant_escalation) -> None:
    """The wrapper is never cancelled, so lane teardown always completes (M5 invariant)."""
    registry = RunRegistry()
    torn_down: list[str] = []
    checkpoint = asyncio.Event()

    async def _stubborn() -> None:
        checkpoint.set()
        await asyncio.sleep(3600)

    async def _teardown() -> None:
        # An await here is the whole point: a cancelled task could not do this reliably.
        await asyncio.sleep(0)
        torn_down.append("lanes")

    entry = registry.submit("r1", _stubborn, teardown=_teardown)
    await checkpoint.wait()
    registry.cancel("r1")
    await _finished(entry)

    assert torn_down == ["lanes"]


@pytest.mark.asyncio
async def test_teardown_runs_after_a_successful_body() -> None:
    registry = RunRegistry()
    calls: list[str] = []

    async def _body() -> None:
        calls.append("body")

    async def _teardown() -> None:
        calls.append("teardown")

    entry = registry.submit("ok", _body, teardown=_teardown)
    await _finished(entry)

    assert calls == ["body", "teardown"]


@pytest.mark.asyncio
async def test_teardown_runs_after_a_failing_body_and_the_error_surfaces() -> None:
    """Teardown ordering was all this asserted before, so a swallowed body error — the
    thing that makes a failed run look like a successful one — went unnoticed."""
    registry = RunRegistry()
    calls: list[str] = []

    async def _boom() -> None:
        raise RuntimeError("planning blew up")

    async def _teardown() -> None:
        calls.append("teardown")

    entry = registry.submit("boom", _boom, teardown=_teardown)
    await _finished(entry)

    assert calls == ["teardown"]
    assert entry.body_task is not None
    exc = entry.body_task.exception()
    assert isinstance(exc, RuntimeError)
    assert str(exc) == "planning blew up"


@pytest.mark.asyncio
async def test_on_admit_runs_once_the_slot_is_won() -> None:
    registry = RunRegistry()
    events: list[str] = []
    gate = asyncio.Event()

    async def _blocker() -> None:
        await gate.wait()

    async def _admit() -> None:
        events.append("admitted")

    async def _body() -> None:
        events.append("body")

    entry_a = registry.submit("a", _blocker)
    entry_b = registry.submit("b", _body, on_admit=_admit)
    await _until(lambda: registry.active_run_id() == "a", "run a to take the slot")

    assert events == []

    gate.set()
    await _finished(entry_a)
    await _finished(entry_b)
    assert events == ["admitted", "body"]


@pytest.mark.asyncio
async def test_cancel_of_unknown_or_finished_run_is_false() -> None:
    registry = RunRegistry()

    async def _body() -> None:
        return None

    assert registry.cancel("nope") is False
    entry = registry.submit("done", _body)
    await _finished(entry)
    assert registry.cancel("done") is False


@pytest.mark.asyncio
async def test_repeated_cancel_arms_only_one_escalation() -> None:
    """A user can click Stop as many times as they like; each click must not leak a task.

    The watchdog sleeps CANCEL_GRACE_S before hard-cancelling, so an unguarded re-arm
    leaves an unreferenced task per click with nothing bounding how many exist.
    """
    registry = RunRegistry()
    release = asyncio.Event()

    async def _body() -> None:
        await release.wait()

    entry = registry.submit("run", _body)
    await _until(lambda: registry.active_run_id() == "run", "run to take the slot")

    assert registry.cancel("run") is True
    first = entry.escalate_task
    assert first is not None

    for _ in range(4):
        assert registry.cancel("run") is True
    assert entry.escalate_task is first

    release.set()
    await _finished(entry)
    registry.reset()


@pytest.mark.asyncio
async def test_reset_cancels_the_escalation_watchdog() -> None:
    """The watchdog sleeps CANCEL_GRACE_S. reset() dropped its entry but left it running,
    so it outlived the registry it belonged to."""
    registry = RunRegistry()
    release = asyncio.Event()

    async def _body() -> None:
        await release.wait()

    entry = registry.submit("run", _body)
    await _until(lambda: registry.active_run_id() == "run", "run to take the slot")
    registry.cancel("run")
    watchdog = entry.escalate_task
    assert watchdog is not None and not watchdog.done()

    registry.reset()
    await _until(lambda: watchdog.done(), "watchdog to stop")

    assert watchdog.cancelled()


@pytest.mark.asyncio
async def test_escalation_clears_itself_once_it_has_fired(instant_escalation) -> None:
    registry = RunRegistry()
    checkpoint = asyncio.Event()

    async def _stubborn() -> None:
        checkpoint.set()
        await asyncio.sleep(3600)

    entry = registry.submit("run", _stubborn)
    await checkpoint.wait()
    registry.cancel("run")
    assert entry.escalate_task is not None

    await _finished(entry)
    await _until(lambda: entry.escalate_task is None, "watchdog to clear itself")
