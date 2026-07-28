"""Wakeup for a run parked on a human decision (roadmap M7, ADR 0015).

The ``diff_reviews`` table is the source of truth for *whether* a review is open; this is
only the notification that lets the parked graph node stop waiting the moment the decisions
handler commits, instead of discovering it on the next poll. Same split as
``EventLog``/``EventBus``.

Process-local, so it inherits the single-worker assumption the console's per-session locks
already rely on (AGENTS.md §4.2). Under two API workers the handler and the parked run can
land in different processes and the notification is lost — the run would then sit until its
timeout. That is a degradation, not a correctness bug, because the table still holds the
decisions.
"""

from __future__ import annotations

import asyncio

_ReviewKey = tuple[str, int]


class ReviewGate:
    """One ``asyncio.Event`` per parked ``(sprint_session_id, attempt)``."""

    def __init__(self) -> None:
        self._waiters: dict[_ReviewKey, asyncio.Event] = {}

    def open(self, sprint_session_id: str, attempt: int) -> asyncio.Event:
        """Register a waiter and return the event the node parks on.

        Replaces any event left by an earlier park of the same key so a stale set flag
        cannot release the new wait immediately.
        """
        event = asyncio.Event()
        self._waiters[(sprint_session_id, attempt)] = event
        return event

    def notify(self, sprint_session_id: str, attempt: int) -> bool:
        """Release the parked run. False when nothing is waiting — a decision recorded
        for a run that already timed out, or one parked in another process."""
        event = self._waiters.get((sprint_session_id, attempt))
        if event is None:
            return False
        event.set()
        return True

    def close(self, sprint_session_id: str, attempt: int) -> None:
        self._waiters.pop((sprint_session_id, attempt), None)

    def waiting(self) -> int:
        """Live waiters. Exposed so a test can assert the table does not leak entries."""
        return len(self._waiters)

    def reset(self) -> None:
        for event in self._waiters.values():
            event.set()
        self._waiters.clear()


_gate: ReviewGate | None = None


def review_gate() -> ReviewGate:
    global _gate
    if _gate is None:
        _gate = ReviewGate()
    return _gate


def reset_review_gate() -> None:
    global _gate
    if _gate is not None:
        _gate.reset()
    _gate = None
