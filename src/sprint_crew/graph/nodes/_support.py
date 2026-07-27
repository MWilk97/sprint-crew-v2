"""State readers, lane/diff helpers, and the phase wrapper shared by the node modules.

Nothing here touches an agent, so it is testable without driving the graph.
"""

from __future__ import annotations

import functools
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from sprint_crew.config import Role
from sprint_crew.graph import lanes
from sprint_crew.graph.state import SprintState
from sprint_crew.orchestrator import workspace_diff as diff_tools
from sprint_crew.orchestrator.acceptance_failure import AcceptanceFailureAnalysis
from sprint_crew.orchestrator.emitter import current_emitter, reset_emitter, set_emitter
from sprint_crew.orchestrator.plan_coverage import PlanCoverageResult
from sprint_crew.orchestrator.run_registry import check_cancelled
from sprint_crew.schemas.session import agent_event as _event
from sprint_crew.schemas.ticket import TaskPlan

# --- state readers -----------------------------------------------------------


def _timed_detail(started: float, **extra: Any) -> dict[str, Any]:
    return {"duration_ms": int((time.monotonic() - started) * 1000), **extra}


def _coverage_from_dict(raw: Any) -> PlanCoverageResult | None:
    if not isinstance(raw, dict):
        return None
    return PlanCoverageResult(
        missing=list(raw.get("missing", [])),
        unexpected=list(raw.get("unexpected", [])),
        out_of_scope_hits=list(raw.get("out_of_scope_hits", [])),
        blocking_unexpected=list(raw.get("blocking_unexpected", [])),
        phantom_paths=list(raw.get("phantom_paths", [])),
        satisfied=bool(raw.get("satisfied", True)),
    )


def _acceptance_failure_dict(analysis: AcceptanceFailureAnalysis | None) -> dict[str, Any]:
    # Always returns a value: acceptance_failure has no reducer, so an omitted key would
    # leave a prior round's failure in state after tests turn green.
    if analysis is None or analysis.kind == "none":
        return {}
    return {
        "kind": analysis.kind,
        "tester_can_help": analysis.tester_can_help,
        "source_paths": list(analysis.source_paths),
        "test_paths": list(analysis.test_paths),
        "summary": analysis.summary,
        "detail_excerpt": analysis.detail_excerpt,
    }


def _coverage_satisfied(state: SprintState) -> bool:
    coverage = state.get("plan_coverage")
    if not isinstance(coverage, dict):
        return True
    return bool(coverage.get("satisfied", True))


def _deadline_epoch(state: SprintState) -> float:
    raw = state.get("deadline_epoch", 0.0)
    try:
        return float(raw or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _deadline_exceeded(state: SprintState) -> bool:
    """True when a per-cycle wall-clock budget is set and already elapsed."""
    deadline = _deadline_epoch(state)
    return deadline > 0.0 and time.time() >= deadline


def _in_backlog_batch(state: SprintState) -> bool:
    return bool(state.get("backlog_run_id"))


# --- lanes and diffs ---------------------------------------------------------


async def _stop_lane_after_cycle(state: SprintState, role: Role) -> None:
    if _in_backlog_batch(state):
        return
    await lanes.stop_lane(role)


async def _swap_lane(stop: Role, start: Role) -> None:
    # One lane at a time on 128 GB unified memory (AGENTS.md §4.1), so every lane change
    # is a stop followed by a start, never a start alone.
    await lanes.stop_lane(stop)
    await lanes.ensure_lane(start)


def _diff_for(state: SprintState, workspace: Path, plan: TaskPlan) -> str:
    """Reuse this cycle's diff, or compute it — recomputing costs a git subprocess per node."""
    return state.get("workspace_diff") or diff_tools.gather_workspace_diff(
        workspace, priority_paths=plan.files_to_touch
    )


# --- phase wrapper -----------------------------------------------------------

_NodeFn = Callable[[SprintState], Awaitable[dict[str, Any]]]


def _phased(phase: str, fn: _NodeFn) -> _NodeFn:
    """Bracket a node with phase_started/phase_completed events, and check cancel (AGENTS.md §5.5).

    Binds the phase onto the context emitter so tool and lane events inside inherit it.
    A no-op when no console run is streaming.
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
            # A timeline saying "completed" for a node that raised is misleading exactly
            # when it is being read to debug that failure. Re-raised untouched.
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
