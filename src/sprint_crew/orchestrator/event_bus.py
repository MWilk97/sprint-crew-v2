"""In-process fan-out for live console event streams (roadmap M3).

The events table (``EventLog``) is the source of truth and the replay path; this bus is
only a live notification layer laid over it. ``EventLog.append_many`` appends-then-publishes,
so a stream subscribes, replays from the table up to the current tail, then receives new
events off its queue — the table and the bus never disagree, and a dropped notification only
costs a reconnect-and-replay, never a lost event.

Invariant: ``asyncio.Queue`` is loop-bound and not thread-safe, so ``publish()`` must run on
the event-loop thread. Keeping publish here, best-effort and loop-local, is what makes that
constraint reviewable in one place.

TODO(bus): the invariant is currently violated on one path. ``api/console/state.emit`` wraps
``append_many`` in ``asyncio.to_thread`` to keep SQLite off the loop, which drags ``publish``
onto the worker with it — so a live subscriber can miss the wakeup for the events the API
layer owns (cancel since M5, review decisions since M7). The in-run emitter is unaffected;
it publishes on the loop by design. See the fix sketched at that call site.
"""

from __future__ import annotations

import asyncio

from sprint_crew.config import get_settings
from sprint_crew.schemas.session import AgentEvent


class EventBus:
    """Fan-out of console events to live SSE subscribers. Bounded, best-effort."""

    def __init__(self, maxsize: int) -> None:
        self._maxsize = maxsize
        self._subscribers: dict[str, set[asyncio.Queue[AgentEvent]]] = {}

    def subscribe(self, console_session_id: str) -> asyncio.Queue[AgentEvent]:
        queue: asyncio.Queue[AgentEvent] = asyncio.Queue(maxsize=self._maxsize)
        self._subscribers.setdefault(console_session_id, set()).add(queue)
        return queue

    def unsubscribe(self, console_session_id: str, queue: asyncio.Queue[AgentEvent]) -> None:
        subs = self._subscribers.get(console_session_id)
        if subs is None:
            return
        subs.discard(queue)
        if not subs:
            self._subscribers.pop(console_session_id, None)

    def publish(self, console_session_id: str, event: AgentEvent) -> None:
        """Notify live subscribers. No-op when none are attached (the common case).

        A full queue means a reader too slow to keep up: drop it rather than grow memory
        unbounded. The dropped stream reconnects and replays the gap from the events table,
        so no event is lost — only its live-push notification.
        """
        subs = self._subscribers.get(console_session_id)
        if not subs:
            return
        for queue in list(subs):
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                self.unsubscribe(console_session_id, queue)

    def subscriber_count(self, console_session_id: str) -> int:
        return len(self._subscribers.get(console_session_id, ()))

    def clear(self) -> None:
        self._subscribers.clear()


_bus: EventBus | None = None


def event_bus() -> EventBus:
    """Process-global bus. One event loop per API worker, so a module singleton is correct."""
    global _bus
    if _bus is None:
        _bus = EventBus(maxsize=get_settings().sse_queue_maxsize)
    return _bus
