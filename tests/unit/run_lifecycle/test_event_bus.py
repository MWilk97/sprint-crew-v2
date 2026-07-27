"""EventBus fan-out: subscribe/unsubscribe, bounded queues, drop-on-overflow (M3)."""

from __future__ import annotations

import asyncio

import pytest

from sprint_crew.orchestrator.event_bus import EventBus
from sprint_crew.schemas.session import AgentEvent


def _ev(name: str) -> AgentEvent:
    return AgentEvent(agent="node", event_type=name, summary=name)


@pytest.mark.asyncio
async def test_publish_reaches_subscriber() -> None:
    bus = EventBus(maxsize=10)
    queue = bus.subscribe("cs-1")
    bus.publish("cs-1", _ev("a"))
    assert (await asyncio.wait_for(queue.get(), timeout=1)).event_type == "a"


@pytest.mark.asyncio
async def test_publish_is_scoped_per_session() -> None:
    bus = EventBus(maxsize=10)
    q1 = bus.subscribe("cs-1")
    q2 = bus.subscribe("cs-2")
    bus.publish("cs-1", _ev("a"))
    assert q1.qsize() == 1
    assert q2.qsize() == 0


@pytest.mark.asyncio
async def test_publish_with_no_subscribers_is_noop() -> None:
    bus = EventBus(maxsize=10)
    bus.publish("cs-nobody", _ev("a"))  # must not raise


@pytest.mark.asyncio
async def test_overflow_drops_slow_subscriber() -> None:
    """A subscriber that never drains is evicted rather than growing memory; it is
    expected to reconnect and replay the gap from the events table."""
    bus = EventBus(maxsize=2)
    bus.subscribe("cs-1")
    for i in range(3):  # third publish overflows the maxsize-2 queue
        bus.publish("cs-1", _ev(f"e{i}"))
    assert bus.subscriber_count("cs-1") == 0


@pytest.mark.asyncio
async def test_unsubscribe_removes_queue() -> None:
    bus = EventBus(maxsize=10)
    queue = bus.subscribe("cs-1")
    bus.unsubscribe("cs-1", queue)
    assert bus.subscriber_count("cs-1") == 0
    bus.publish("cs-1", _ev("a"))  # no error after last subscriber leaves
