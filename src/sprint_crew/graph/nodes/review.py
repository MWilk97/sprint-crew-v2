"""Review, the deterministic merge gate, and the human review gate that follows it."""

from __future__ import annotations

import asyncio
import json
import time
from datetime import UTC, datetime, timedelta
from typing import Any

from sprint_crew.agents import reviewer
from sprint_crew.config import Role, get_settings
from sprint_crew.graph import lanes
from sprint_crew.graph.nodes._support import (
    _coverage_satisfied,
    _diff_for,
    _stop_lane_after_cycle,
    _swap_lane,
    _timed_detail,
)
from sprint_crew.graph.state import (
    SprintState,
    code_change_from_state,
    task_plan_from_state,
    ticket_from_state,
    workspace_from_state,
)
from sprint_crew.orchestrator.diff_capture import record_diff_snapshot
from sprint_crew.orchestrator.diff_store import diff_store
from sprint_crew.orchestrator.emitter import current_emitter
from sprint_crew.orchestrator.merge_gate import review_accepted
from sprint_crew.orchestrator.review_gate import review_gate
from sprint_crew.orchestrator.run_registry import check_cancelled
from sprint_crew.schemas.change import ReviewOutcome
from sprint_crew.schemas.session import agent_event as _event
from sprint_crew.schemas.session import utc_now_iso


async def review(state: SprintState) -> dict[str, Any]:
    started = time.monotonic()
    await _swap_lane(Role.CODING, Role.WORK)
    plan = task_plan_from_state(state)
    change = code_change_from_state(state)
    workspace = workspace_from_state(state)
    workspace_diff = _diff_for(state, workspace, plan)
    # Before the Reviewer call, not after: this is the last point where the working tree
    # still holds the uncommitted change (ship stages everything), and the Reviewer takes
    # minutes, so capturing first is what puts the diff in front of the user while the
    # review is still running.
    await record_diff_snapshot(
        workspace,
        sprint_session_id=state["session_id"],
        ticket_key=plan.ticket_key,
        attempt=state.get("attempt", 0),
    )
    test_additions_json = ""
    if raw_additions := state.get("test_additions"):
        test_additions_json = json.dumps(raw_additions, indent=2)

    coverage = state.get("plan_coverage", {})
    coverage_summary = ""
    if isinstance(coverage, dict) and not coverage.get("satisfied", True):
        coverage_summary = (
            f"missing={coverage.get('missing', [])}; unexpected={coverage.get('unexpected', [])}"
        )

    tests_already_run = bool(state.get("tests_run_this_cycle", False) and change.tests_passed)
    outcome = await reviewer.run_reviewer(
        plan,
        change,
        workspace,
        workspace_diff=workspace_diff,
        test_additions_json=test_additions_json,
        ticket_acceptance_criteria=ticket_from_state(state).acceptance_criteria,
        tests_already_run=tests_already_run,
        coverage_summary=coverage_summary,
        files_to_touch=plan.files_to_touch,
    )
    await _stop_lane_after_cycle(state, Role.WORK)
    return {
        "review_outcome": outcome.model_dump(),
        "workspace_diff": workspace_diff,
        "events": [
            _event(
                "reviewer",
                "review_complete",
                f"ReviewOutcome passed={outcome.passed} tests_passed={outcome.tests_passed}",
                findings=len(outcome.findings),
                **_timed_detail(started, lane="work"),
            ),
        ],
    }


async def merge_gate(state: SprintState) -> dict[str, Any]:
    started = time.monotonic()
    outcome = ReviewOutcome.model_validate(state["review_outcome"])
    coverage_ok = _coverage_satisfied(state)
    accepted = review_accepted(outcome, coverage_satisfied=coverage_ok)
    block_reason: str | None = None
    if not accepted:
        if not coverage_ok:
            block_reason = "coverage"
        elif not outcome.tests_passed:
            block_reason = "tests"
        elif not outcome.passed:
            block_reason = "review"
        else:
            block_reason = "unknown"
    return {
        "events": [
            _event(
                "merge_gate",
                "gate_result",
                "accepted" if accepted else "rejected",
                accepted=accepted,
                attempt=state.get("attempt", 0),
                coverage_satisfied=coverage_ok,
                block_reason=block_reason,
                **_timed_detail(started),
            ),
        ],
    }


# --- human review gate (M7) --------------------------------------------------
# Sits after the merge gate, never in place of it: the deterministic gates run over the
# whole tree first (AGENTS.md §8.1), and the user only ever sees a change the pipeline
# already accepted. Rejection is feedback into the existing retry loop, not a partial
# commit — see the roadmap's M7 section for why a subset commit is a trap.

# How often the park re-checks the cancel token. A hard cancel arrives as CancelledError
# through the wait itself; the cooperative token is only ever read by a checkpoint, and
# this is the only checkpoint that runs while parked.
_CANCEL_TICK_S = 1.0


async def _wait_for_decision(waiter: asyncio.Event, timeout_s: float) -> bool:
    """Park until the decisions handler releases us. False means the budget elapsed."""
    deadline = time.monotonic() + timeout_s
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        try:
            await asyncio.wait_for(waiter.wait(), timeout=min(_CANCEL_TICK_S, remaining))
        except TimeoutError:
            check_cancelled()
            continue
        return True


async def await_diff_review(state: SprintState) -> dict[str, Any]:
    """Block the run on a per-file human verdict before anything ships (ADR 0015)."""
    emitter = current_emitter()
    settings = get_settings()
    if emitter is None or not settings.console_review_gate_enabled:
        # No console session behind this run — a direct /sprint/* call, smoke_cycle, or a
        # unit test. Nobody could answer, so parking would hang the cycle.
        return {}

    console_session_id = emitter.console_session_id
    sprint_session_id = state["session_id"]
    attempt = state.get("attempt", 0)
    store = diff_store()
    snapshot = await asyncio.to_thread(store.get, console_session_id, sprint_session_id, attempt)
    if snapshot is None or not snapshot.files:
        # Capture is best-effort and never fails a cycle, so a missing snapshot means there
        # is nothing to review — not that the user declined to.
        return {}

    # A review may sit for up to DIFF_REVIEW_TIMEOUT_S. Inside a backlog batch nothing else
    # stops a lane between stories, and idling a 23 GB model for an hour costs more than
    # the reload the retry or the next story pays (AGENTS.md §4.1).
    await lanes.stop_all_lanes()

    now = datetime.now(tz=UTC)
    expires_at = (now + timedelta(seconds=settings.diff_review_timeout_s)).isoformat()
    rounds = state.get("user_rejection_rounds", 0)
    waiter = review_gate().open(sprint_session_id, attempt)
    await asyncio.to_thread(
        store.open_review,
        console_session_id,
        sprint_session_id,
        attempt,
        rejection_round=rounds,
        requested_at=now.isoformat(),
        expires_at=expires_at,
    )
    emitter.emit(
        _event(
            "orchestrator",
            "awaiting_diff_review",
            f"{len(snapshot.files)} file(s) awaiting review",
            level="warning",
            sprint_session_id=sprint_session_id,
            attempt=attempt,
            files_changed=len(snapshot.files),
            rejection_round=rounds,
            expires_at=expires_at,
        )
    )
    try:
        decided = await _wait_for_decision(waiter, settings.diff_review_timeout_s)
    finally:
        review_gate().close(sprint_session_id, attempt)
        # Pending-only in the store, so a decided review keeps its outcome and a run that
        # unwound (timeout, Stop) leaves no open review the console would keep reporting.
        await asyncio.to_thread(
            store.close_review,
            console_session_id,
            sprint_session_id,
            attempt,
            status="expired",
            decided_at=utc_now_iso(),
        )

    if not decided:
        summary = "Diff review expired without a decision"
        emitter.emit(
            _event(
                "orchestrator",
                "diff_review_expired",
                summary,
                level="error",
                attempt=attempt,
                timeout_s=settings.diff_review_timeout_s,
            )
        )
        return {
            "error": f"{summary} after {settings.diff_review_timeout_s:.0f}s",
            "failure_reason": "review_timeout",
        }

    decided_review = await asyncio.to_thread(
        store.get_review, console_session_id, sprint_session_id, attempt
    )
    decisions = decided_review.decisions if decided_review is not None else []
    rejected = [d for d in decisions if d.decision == "reject"]
    if rejected and rounds >= settings.max_user_rejection_rounds:
        summary = f"Rejection budget exhausted after {rounds} round(s)"
        emitter.emit(
            _event(
                "orchestrator",
                "review_budget_exhausted",
                summary,
                level="error",
                attempt=attempt,
                rejected=[d.path for d in rejected],
                max_rounds=settings.max_user_rejection_rounds,
            )
        )
        return {"error": summary, "failure_reason": "review_budget_exhausted"}

    return {"review_decisions": [d.model_dump(mode="json") for d in decisions]}
