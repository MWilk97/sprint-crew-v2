"""RunRegistry: single-slot admission, queue positions, and both cancel paths (M5)."""

from __future__ import annotations

import asyncio

import pytest

from sprint_crew.orchestrator.run_registry import (
    CancelToken,
    RunCancelled,
    RunRegistry,
    check_cancelled,
    current_cancel_token,
)


async def _settle() -> None:
    """Yield long enough for freshly created tasks to reach their first await."""
    for _ in range(5):
        await asyncio.sleep(0)


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

    registry.submit("a", body("a"))
    registry.submit("b", body("b"))
    await _settle()

    # b is parked on the semaphore: it has not started, and reports one run ahead of it.
    assert order == ["a:start"]
    assert registry.position("b") == 1
    assert registry.position("a") is None
    assert registry.active_run_id() == "a"

    gates["a"].set()
    await _settle()
    gates["b"].set()
    await _settle()

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

    registry.submit("solo", _body)
    # Before its task has even run: still in _waiting, but nothing is ahead.
    assert registry.position("solo") == 0

    await _settle()
    assert registry.position("solo") is None
    gate.set()
    await _settle()


@pytest.mark.asyncio
async def test_body_sees_the_cancel_token_in_context() -> None:
    registry = RunRegistry()
    seen: list[CancelToken | None] = []

    async def _body() -> None:
        seen.append(current_cancel_token())

    entry = registry.submit("r1", _body)
    await _settle()
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

    registry.submit("a", _blocker)
    registry.submit("b", _second)
    await _settle()

    assert registry.cancel("b", reason="user stopped") is True
    gate.set()
    await _settle()

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

    registry.submit("r1", _body)
    await checkpoint.wait()

    registry.cancel("r1", reason="user stopped")
    await _settle()

    assert outcome == ["cancelled:user stopped"]


@pytest.mark.asyncio
async def test_cooperative_cancel_does_not_hard_cancel_within_the_grace_window(
    monkeypatch,
) -> None:
    """The watchdog must not fire while the body is still unwinding cooperatively."""
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

    registry.submit("r1", _body)
    await checkpoint.wait()
    registry.cancel("r1")
    await _settle()

    assert hard_cancelled == []
    entry = registry.get("r1")
    assert entry is not None and entry.state == "cancelling"


# --- cancel: running, hard escalation ----------------------------------------


@pytest.mark.asyncio
async def test_hard_escalation_cancels_a_body_that_ignores_the_token(monkeypatch) -> None:
    """A body blocked in a subprocess never sees the token; the watchdog must stop it."""
    from sprint_crew import config

    monkeypatch.setattr(
        config.get_settings(),
        "cancel_grace_s",
        0.0,
        raising=False,
    )
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

    registry.submit("r1", _stubborn)
    await checkpoint.wait()
    registry.cancel("r1")
    await _settle()

    assert hard_cancelled == [True]


@pytest.mark.asyncio
async def test_hard_cancel_still_runs_teardown(monkeypatch) -> None:
    """The wrapper is never cancelled, so lane teardown always completes (M5 invariant)."""
    from sprint_crew import config

    monkeypatch.setattr(config.get_settings(), "cancel_grace_s", 0.0, raising=False)
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

    registry.submit("r1", _stubborn, teardown=_teardown)
    await checkpoint.wait()
    registry.cancel("r1")
    await _settle()

    assert torn_down == ["lanes"]


@pytest.mark.asyncio
async def test_teardown_runs_on_success_and_on_failure() -> None:
    registry = RunRegistry()
    calls: list[str] = []

    async def _ok() -> None:
        calls.append("ok-body")

    async def _boom() -> None:
        raise RuntimeError("planning blew up")

    async def _teardown(tag: str):
        async def _inner() -> None:
            calls.append(f"teardown-{tag}")

        return _inner

    registry.submit("ok", _ok, teardown=await _teardown("ok"))
    await _settle()
    registry.submit("boom", _boom, teardown=await _teardown("boom"))
    await _settle()

    assert calls == ["ok-body", "teardown-ok", "teardown-boom"]


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

    registry.submit("a", _blocker)
    registry.submit("b", _body, on_admit=_admit)
    await _settle()

    assert events == []

    gate.set()
    await _settle()
    assert events == ["admitted", "body"]


@pytest.mark.asyncio
async def test_cancel_of_unknown_or_finished_run_is_false() -> None:
    registry = RunRegistry()

    async def _body() -> None:
        return None

    assert registry.cancel("nope") is False
    registry.submit("done", _body)
    await _settle()
    assert registry.cancel("done") is False
