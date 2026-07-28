"""Plan mode as a real run, and the handoff from a plan to the code run (roadmap M10).

Plan mode was the last synchronous thing in the console: ``POST /start`` built a stub result
inline and returned ``completed``. It now goes through the ``RunRegistry`` like everything
else, which is not ceremony — it needs the Work lane, and the lane is single-tenant. Taking
the one admission slot is what stops a plan run and a code run from thrashing lane swaps,
and it makes queueing, cancel and the event stream work for plan mode for free.

**Promote creates a second session rather than reopening the first.** A completed session is
terminal, and the reaper, ``complete: true`` on the events stream, and every client that
stops polling on a terminal status all rely on that. The plan stays readable while the code
run it produced executes, and the two show up as two rows in history — which is what they
are. The child runs the *stored* ``BacklogPlan``, so the backlog that executes is the one
that was reviewed, not a second opinion from a re-run ScrumMaster.
"""

from __future__ import annotations

import asyncio
import logging
from uuid import uuid4

from fastapi import HTTPException

from sprint_crew.api.console.state import (
    _lock_for,
    arun_console_reaper,
    emit_for,
    load_session,
    require_session,
    router,
    touch,
)
from sprint_crew.api.console.workspace import schedule_workspace_prep
from sprint_crew.config import get_settings
from sprint_crew.graph.lanes import stop_all_lanes
from sprint_crew.orchestrator.emitter import Emitter, emit_live, reset_emitter, set_emitter
from sprint_crew.orchestrator.event_log import event_log
from sprint_crew.orchestrator.plan_run import await_workspace_ready, run_plan
from sprint_crew.orchestrator.plan_store import StoredPlan, plan_store
from sprint_crew.orchestrator.run_registry import RunCancelled, run_registry
from sprint_crew.schemas.console import (
    ConsoleMessage,
    ConsoleMode,
    ConsolePlanResult,
    ConsoleSession,
    ConsoleSessionStatus,
    PlanDepth,
    SprintRunRef,
)
from sprint_crew.schemas.session import agent_event

logger = logging.getLogger(__name__)

_PLANNING_PHASE = "planning"


def resolve_depth(requested: PlanDepth | None) -> PlanDepth:
    return requested or PlanDepth(get_settings().console_plan_default_depth)


def submit_plan_run(session: ConsoleSession, *, depth: PlanDepth, prompt: str) -> None:
    """Queue a plan run and mark the session queued/running. Caller holds the lock.

    Submitting under the lock is safe: the body cannot reach its first ``_lock_for`` until
    this handler returns, and it parks on the admission semaphore before that anyway.
    """
    plan_run_id = f"plan-{uuid4().hex[:8]}"
    session.plan_run_id = plan_run_id
    session.plan_result = None
    session.error = None

    registry = run_registry()
    registry.submit(
        plan_run_id,
        lambda: _plan_body(
            session_id=session.session_id,
            plan_run_id=plan_run_id,
            prompt=prompt,
            depth=depth,
        ),
        console_session_id=session.session_id,
        # Repeated from run_plan's own finally, which a hard cancel can interrupt mid-await.
        # Idempotent, and a lane left loaded wedges the GPU for everything else.
        teardown=stop_all_lanes,
    )
    position = registry.position(plan_run_id)
    session.status = ConsoleSessionStatus.QUEUED if position else ConsoleSessionStatus.RUNNING
    session.queue_position = position or None


async def _plan_body(*, session_id: str, plan_run_id: str, prompt: str, depth: PlanDepth) -> None:
    """The plan run, executed by the RunRegistry once it holds the lane permit."""
    emitter = Emitter(event_log(), session_id, None, phase=_PLANNING_PHASE)
    token = set_emitter(emitter)
    try:
        workspace = await await_workspace_ready(session_id)
        result, plan = await run_plan(
            prompt=prompt,
            workspace_root=workspace,
            session_id=session_id,
            depth=depth,
        )
        # Stored before the session is marked complete: a Promote that raced a completion
        # must never find a finished plan with no backlog behind it.
        await asyncio.to_thread(
            plan_store().save,
            StoredPlan(session_id=session_id, plan=plan, prompt=prompt),
        )
        # Emitted before the status flip, not after: a client that stops polling once the
        # session is terminal would otherwise race past the event that says why.
        emit_live(
            agent_event(
                "orchestrator",
                "plan_complete",
                f"Plan ready: {len(result.stories)} story/ies",
                depth=depth.value,
                story_count=len(result.stories),
                recommended_first=result.recommended_first,
            )
        )
        await _finish(session_id, plan_run_id, ConsoleSessionStatus.COMPLETED, plan_result=result)
    except (RunCancelled, asyncio.CancelledError) as exc:
        # Plan mode has no backlog run for the console to derive a terminal status from,
        # so unlike a code run the body owns this flip itself.
        await _finish(session_id, plan_run_id, ConsoleSessionStatus.CANCELLED, error=str(exc))
        raise
    except Exception as exc:
        logger.exception("plan run %s failed for session %s", plan_run_id, session_id)
        emit_live(
            agent_event(
                "orchestrator",
                "plan_failed",
                "Could not produce a plan",
                level="error",
                error=str(exc)[:500],
            )
        )
        await _finish(session_id, plan_run_id, ConsoleSessionStatus.FAILED, error=str(exc)[:500])
    finally:
        reset_emitter(token)


async def _finish(
    session_id: str,
    plan_run_id: str,
    status: ConsoleSessionStatus,
    *,
    plan_result: ConsolePlanResult | None = None,
    error: str | None = None,
) -> None:
    """Write the outcome under the lock, re-reading first.

    Guarded on ``plan_run_id`` the same way ``_clear_ask`` is: a session that was cancelled
    and re-started must not have its second run's status stomped by its first run's exit.
    """
    async with _lock_for(session_id):
        session = await load_session(session_id)
        if session is None or session.plan_run_id != plan_run_id:
            return  # deleted, reaped, or superseded by a newer plan run
        session.status = status
        session.queue_position = None
        if plan_result is not None:
            session.plan_result = plan_result
        if error is not None:
            session.error = error
        await touch(session)
    await arun_console_reaper()


@router.post("/sessions/{id}/promote", response_model=ConsoleSession, status_code=202)
async def promote_console_session(id: str) -> ConsoleSession:
    """Turn a reviewed plan into a code run, skipping clarify. Returns the *new* session.

    The click is the confirmation — the user already read the backlog — so the child is
    created ``ready`` and confirmed, and its run is queued before this returns.
    """
    await require_session(id)
    async with _lock_for(id):
        parent = await require_session(id)
        if parent.mode is not ConsoleMode.PLAN:
            raise HTTPException(
                status_code=409, detail=f"session is mode {parent.mode.value}; promote needs plan"
            )
        if parent.status is not ConsoleSessionStatus.COMPLETED or parent.plan_result is None:
            raise HTTPException(
                status_code=409,
                detail=f"session is {parent.status.value}; promote needs a completed plan",
            )
        stored = await asyncio.to_thread(plan_store().load, id)
        if stored is None:
            raise HTTPException(
                status_code=409,
                detail="the reviewed backlog is no longer stored; plan again before promoting",
            )

    child = ConsoleSession(
        session_id=f"cs-{uuid4().hex[:8]}",
        mode=ConsoleMode.CODE,
        status=ConsoleSessionStatus.READY,
        confirmed=True,
        repo_url=parent.repo_url,
        target_language=parent.target_language,
        parent_session_id=parent.session_id,
        # The conversation that produced the plan, carried over so the code session reads as
        # a continuation. Clarify is not re-run: those questions were answered upstream.
        messages=[ConsoleMessage(**m.model_dump()) for m in parent.messages],
        intent=parent.intent,
        prior_clarifications=list(parent.prior_clarifications),
    )

    # Lazy import: app.py imports this router at module load.
    from sprint_crew.api.app import start_from_prompt_run

    async with _lock_for(child.session_id):
        schedule_workspace_prep(child)
        await touch(child)
        try:
            run_id = await start_from_prompt_run(
                prompt=stored.prompt,
                repo_url=child.repo_url,
                console_session_id=child.session_id,
                plan=stored.plan,
            )
        except Exception as exc:
            child.status = ConsoleSessionStatus.FAILED
            child.error = str(exc)
            await touch(child)
            raise
        child.sprint_ref = SprintRunRef(backlog_run_id=run_id)
        position = run_registry().position(run_id)
        child.status = ConsoleSessionStatus.QUEUED if position else ConsoleSessionStatus.RUNNING
        child.queue_position = position or None
        await touch(child)

    await emit_for(
        parent.session_id,
        agent_event(
            "orchestrator",
            "promoted",
            "Plan promoted to a code run",
            child_session_id=child.session_id,
            run_id=run_id,
            story_count=len(stored.plan.stories),
        ),
    )
    return child
