"""Live event emission seam for fine-grained progress (roadmap M4).

The events table (``EventLog``) is the source of truth; ``_persist_progress`` flushes
per-node deltas at node boundaries (M2). That is too coarse for tool calls, lane loads,
and phase transitions, which need to reach the table — and the SSE bus — the moment they
happen, mid-node. This module is the seam that lets deep code (tool dispatch, lane control,
graph nodes) publish an event live without threading a callback through six signatures.

An ``Emitter`` wraps the ``EventLog`` plus the console/sprint id pair and delegates to the
existing ``append_many`` (so seq allocation and bus publish are unchanged). It is stashed in
a ``ContextVar`` set once per sprint cycle in ``create_and_run_cycle``; the contextvar
propagates through the async call stack and is copied into ``asyncio.to_thread`` workers.

Reconciliation with the batch path: an event published here comes back seq-stamped, and that
``seq`` is carried on the tool-log entry so the node-end conversion rebuilds it stamped too.
``_persist_progress`` therefore drops anything that already has a ``seq`` from its delta —
identity, not event type, so a new live event kind needs no change there. When no emitter is
set — plain sprint runs, most unit tests — ``seq`` stays ``None`` and the batch path writes
everything as before, so the fallback is preserved.

Invariant inherited from ``EventBus``: ``publish()`` (inside ``append_many``) must run on the
event-loop thread. Every emit site today runs on the loop (tool dispatch, node bodies, lane
control awaited from the loop), so this holds. Moving any emit site onto a worker thread would
break it — see ``event_bus`` for the ``call_soon_threadsafe`` escape hatch.
"""

from __future__ import annotations

from contextvars import ContextVar, Token
from dataclasses import dataclass, replace

from sprint_crew.orchestrator.event_log import EventLog
from sprint_crew.schemas.session import AgentEvent


@dataclass(frozen=True)
class Emitter:
    """Publishes a single event live to the console-scoped timeline and SSE bus."""

    log: EventLog
    console_session_id: str
    sprint_session_id: str | None
    phase: str | None = None

    def emit(self, event: AgentEvent) -> AgentEvent:
        """Append one event under the next seq and notify live subscribers.

        Returns the seq-stamped copy — callers that also route the event through graph
        state keep that ``seq`` so the node-end path can tell it was already published.
        The bound ``phase`` fills in when the event does not carry its own, so tool and
        lane events inherit the enclosing node's phase.
        """
        if self.phase is not None and event.phase is None:
            event = event.model_copy(update={"phase": self.phase})
        (stamped,) = self.log.append_many(self.console_session_id, self.sprint_session_id, [event])
        return stamped

    def with_phase(self, phase: str) -> Emitter:
        """A copy bound to ``phase`` for the duration of one graph node."""
        return replace(self, phase=phase)


_current_emitter: ContextVar[Emitter | None] = ContextVar("_current_emitter", default=None)


def current_emitter() -> Emitter | None:
    return _current_emitter.get()


def emit_live(event: AgentEvent) -> AgentEvent | None:
    """Emit if a console run is streaming, else do nothing. The common no-op case.

    Returns the seq-stamped event when it was published, ``None`` when no emitter is
    installed — which is how a caller tells whether the event still needs persisting
    by the node-end batch path.
    """
    emitter = _current_emitter.get()
    return emitter.emit(event) if emitter is not None else None


def set_emitter(emitter: Emitter | None) -> Token[Emitter | None]:
    """Set the process-context emitter; returns the token for ``reset_emitter``."""
    return _current_emitter.set(emitter)


def reset_emitter(token: Token[Emitter | None]) -> None:
    _current_emitter.reset(token)
