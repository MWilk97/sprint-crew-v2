"""The node wrapper that emits phase events and holds the graph's cancel checkpoint.

Isolated from pipeline.py because it is the subtlest code in the graph: it is the single
place a cancel is observed, and the only reason the routing functions need no check of
their own.
"""

from __future__ import annotations

import functools
import time
from collections.abc import Awaitable, Callable
from typing import Any

from sprint_crew.graph.state import SprintState
from sprint_crew.orchestrator.emitter import current_emitter, reset_emitter, set_emitter
from sprint_crew.orchestrator.run_registry import check_cancelled
from sprint_crew.schemas.session import agent_event as _event

_NodeFn = Callable[[SprintState], Awaitable[dict[str, Any]]]


def _phased(phase: str, fn: _NodeFn) -> _NodeFn:
    """Bracket a graph node with live ``phase_started`` / ``phase_completed`` events (M4).

    Binds the enclosing phase onto the context emitter for the node's duration, so tool and
    lane events emitted inside inherit ``phase``. A no-op when no console run is streaming.

    Also the graph's single cancel checkpoint (M5). Firing here covers every node *and*
    every routing decision — a route runs between two phased nodes, so a cancel requested
    during routing is caught on the next node's entry. That is why the routing functions
    carry no check of their own.
    """

    @functools.wraps(fn)
    async def _wrapped(state: SprintState) -> dict[str, Any]:
        check_cancelled()
        emitter = current_emitter()
        if emitter is None:
            return await fn(state)
        phased = emitter.with_phase(phase)
        token = set_emitter(phased)
        phased.emit(_event("orchestrator", "phase_started", f"{phase} started", level="debug"))
        started = time.monotonic()
        ok = True
        try:
            return await fn(state)
        except BaseException:
            # Report the phase as failed rather than completed: a timeline that says
            # "completed" for a node that raised is misleading exactly when it is being
            # read to debug that failure. Re-raised untouched.
            ok = False
            raise
        finally:
            phased.emit(
                _event(
                    "orchestrator",
                    "phase_completed",
                    f"{phase} {'completed' if ok else 'failed'}",
                    duration_ms=round((time.monotonic() - started) * 1000, 1),
                    ok=ok,
                    level="debug" if ok else "error",
                )
            )
            reset_emitter(token)

    return _wrapped
