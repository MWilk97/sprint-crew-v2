"""The from-prompt run body: everything that used to block ``POST /start`` (roadmap M5).

``start_from_prompt_run`` did workspace clone, vector index, context enrichment, a Work-lane
load (1200 s health budget) and the full ScrumMaster call *inline* before returning a run id.
Only the batched execution was deferred. So "start" meant "wait out the whole planning phase".

All of it moves here, into the body the RunRegistry runs after admission. The handler now
allocates ids and returns; progress arrives on the event stream instead. That is only useful
because the emitter is installed *before* the prep work — otherwise the newly non-blocking
start would just replace a long wait with a silent one.
"""

from __future__ import annotations

import asyncio
import logging

from sprint_crew.agents.scrum_master import run_scrum_master
from sprint_crew.config import Role
from sprint_crew.graph.lanes import ensure_lane, stop_lane
from sprint_crew.orchestrator.batch_cycle import run_backlog_batched
from sprint_crew.orchestrator.emitter import Emitter, emit_live, reset_emitter, set_emitter
from sprint_crew.orchestrator.event_log import event_log
from sprint_crew.orchestrator.repo_context import enrich_repo_context, maybe_index_workspace
from sprint_crew.orchestrator.session import prepare_workspace
from sprint_crew.schemas.backlog import BacklogPlan
from sprint_crew.schemas.session import BacklogRun, agent_event

logger = logging.getLogger(__name__)

_PLANNING_PHASE = "planning"


async def run_from_prompt(
    *,
    run_id: str,
    prompt: str,
    repo_url: str | None,
    use_real_ship: bool,
    console_session_id: str | None = None,
) -> BacklogRun:
    """Plan then execute a from-prompt run. Long-running; call from the RunRegistry."""
    workspace_id = f"backlog-{run_id}"
    # No sprint session exists yet — planning happens before the first story's cycle — so the
    # emitter is bound with sprint_session_id=None. create_and_run_cycle installs its own
    # per-story emitter later and restores this one on the way out.
    emitter = (
        Emitter(
            log=event_log(),
            console_session_id=console_session_id,
            sprint_session_id=None,
        ).with_phase(_PLANNING_PHASE)
        if console_session_id
        else None
    )
    token = set_emitter(emitter)
    try:
        plan = await _plan(workspace_id, prompt, repo_url)
        return await run_backlog_batched(
            run_id=run_id,
            plan=plan,
            user_prompt=prompt,
            repo_url=repo_url,
            use_real_ship=use_real_ship,
            console_session_id=console_session_id,
        )
    finally:
        reset_emitter(token)


async def _plan(workspace_id: str, prompt: str, repo_url: str | None) -> BacklogPlan:
    # to_thread: clone, indexing, and context enrichment are blocking I/O; keep them off the
    # event loop so /health and any live SSE stream stay responsive during planning (M3).
    workspace = await asyncio.to_thread(prepare_workspace, workspace_id, repo_url=repo_url)
    emit_live(
        agent_event(
            "orchestrator",
            "workspace_ready",
            "Workspace prepared",
            workspace_root=str(workspace),
            repo_url=repo_url,
        )
    )
    await asyncio.to_thread(maybe_index_workspace, workspace, workspace_id, prompt=prompt)
    repo_context = await asyncio.to_thread(enrich_repo_context, workspace, workspace_id, prompt)

    work_lane = Role.WORK
    await ensure_lane(work_lane)
    try:
        plan = await run_scrum_master(
            user_prompt=prompt,
            repo_context=repo_context,
            role=work_lane,
        )
    finally:
        await stop_lane(work_lane)

    emit_live(
        agent_event(
            "orchestrator",
            "backlog_planned",
            f"Backlog planned: {len(plan.stories)} story/ies",
            story_count=len(plan.stories),
            story_keys=[s.key for s in plan.stories],
            recommended_first=plan.recommended_first,
        )
    )
    return plan
