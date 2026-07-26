"""Pure state readers and small lane/diff helpers for the graph nodes.

Extracted from pipeline.py so the node functions read as pipeline stages rather than as
stages interleaved with state plumbing. Nothing here touches an agent or the event log,
which is what makes it testable without driving the graph.
"""

from __future__ import annotations

import time
from typing import Any

from sprint_crew.graph.state import SprintState
from sprint_crew.orchestrator.acceptance_failure import AcceptanceFailureAnalysis
from sprint_crew.orchestrator.plan_coverage import PlanCoverageResult


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
    """Serialize this round's acceptance-failure analysis for graph state.

    Always returns a value (empty dict when there's no failure) so a node's
    returned update overwrites a stale failure recorded in an earlier round —
    acceptance_failure has no reducer, so an omitted key would otherwise leave
    a prior round's failure in state after tests turn green.
    """
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
