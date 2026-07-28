"""Real plan mode: analysis that never ships (roadmap M10, ADR 0012).

Plan mode used to be string formatting — ``build_plan_result`` echoed the prompt and the
clarify answers back as story titles, with no model call and no repo access. This module
replaces it with the same agents the code path uses, run read-only: ScrumMaster for the
backlog, and at ``deep`` depth the TechLead per story for real ``files_to_touch`` /
``steps`` / ``acceptance_tests``.

**Nothing here writes.** No branch, no commit, no PR, and — the easy one to get wrong — no
Jira. ``create_jira_tickets`` (``orchestrator/backlog.py``) calls ``jira.create_issue`` per
story, so plan mode builds its tickets locally with ``ticket_from_story`` instead. The
TechLead is already read-only by construction (``workspace_deps(mutate=False)`` plus
``build_readonly_toolset``), so the invariant holds structurally rather than by convention.

The session's own checkout is what gets analysed (M8), so plan mode clones nothing and
starts against an index that is usually already warm.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from sprint_crew.agents.scrum_master import run_scrum_master
from sprint_crew.agents.tech_lead import run_tech_lead
from sprint_crew.config import Role, get_settings
from sprint_crew.graph.lanes import ensure_lane, stop_lane
from sprint_crew.orchestrator.complexity import assess_ticket_complexity
from sprint_crew.orchestrator.emitter import emit_live
from sprint_crew.orchestrator.repo_context import enrich_repo_context
from sprint_crew.orchestrator.run_registry import check_cancelled
from sprint_crew.schemas.backlog import BacklogPlan, BacklogStory
from sprint_crew.schemas.console import ConsolePlanResult, PlanDepth, PlanPreviewStory
from sprint_crew.schemas.session import agent_event
from sprint_crew.schemas.ticket import JiraTicket, TaskPlan
from sprint_crew.vector.scope import index_scope_for

logger = logging.getLogger(__name__)


def ticket_from_story(story: BacklogStory) -> JiraTicket:
    """A ticket for the TechLead to plan against, created nowhere but in memory.

    Deliberately not ``create_jira_tickets``: that one really does POST to Jira, and plan
    mode's whole promise is that reading a plan costs nothing outside this process.
    """
    return JiraTicket(
        key=story.key,
        summary=story.summary,
        description=story.description,
        status="To Do",
        issue_type=story.issue_type.value,
        acceptance_criteria=story.acceptance_criteria,
    )


async def run_plan(
    *,
    prompt: str,
    workspace_root: Path,
    session_id: str,
    depth: PlanDepth,
) -> tuple[ConsolePlanResult, BacklogPlan]:
    """Plan a backlog against the session's checkout. Long-running; call from the registry.

    Returns the client-facing result and the raw ``BacklogPlan`` behind it — the caller
    persists the second so Promote can execute exactly this backlog.
    """
    emit_live(
        agent_event(
            "orchestrator",
            "plan_started",
            "Planning" if depth is PlanDepth.QUICK else "Planning in detail",
            depth=depth.value,
            workspace_root=str(workspace_root),
        )
    )
    # A read-only pass over a pristine session checkout: the shared repo index is the right
    # tier and there is no overlay, because plan mode never edits a file.
    scope = index_scope_for(workspace_root)
    repo_context = await asyncio.to_thread(enrich_repo_context, workspace_root, scope, prompt)

    await ensure_lane(Role.WORK)
    try:
        plan = await run_scrum_master(
            user_prompt=prompt,
            repo_context=repo_context,
            role=Role.WORK,
        )
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
        stories = await _preview_stories(
            plan,
            workspace_root=workspace_root,
            session_id=session_id,
            repo_context=repo_context,
            depth=depth,
        )
    finally:
        await stop_lane(Role.WORK)

    result = ConsolePlanResult(
        summary=plan.product_brief.summary,
        stories=stories,
        depth=depth,
        product_brief=plan.product_brief,
        recommended_first=plan.recommended_first,
    )
    return result, plan


async def _preview_stories(
    plan: BacklogPlan,
    *,
    workspace_root: Path,
    session_id: str,
    repo_context: str,
    depth: PlanDepth,
) -> list[PlanPreviewStory]:
    previews: list[PlanPreviewStory] = []
    for story in plan.stories:
        # Between stories, like the batch loop: each TechLead pass is a model run, and a
        # Stop that only lands after all five is not a Stop.
        check_cancelled()
        ticket = ticket_from_story(story)
        preview = _preview(story, ticket)
        if depth is PlanDepth.DEEP:
            preview = await _detail(
                preview,
                ticket=ticket,
                workspace_root=workspace_root,
                session_id=session_id,
                repo_context=repo_context,
            )
        previews.append(preview)
        emit_live(
            agent_event(
                "orchestrator",
                "story_planned",
                f"{story.key}: {story.summary}",
                key=story.key,
                depth=depth.value,
                files_to_touch=preview.files_to_touch,
                planning_mode=preview.planning_mode,
            )
        )
    return previews


def _preview(story: BacklogStory, ticket: JiraTicket) -> PlanPreviewStory:
    return PlanPreviewStory(
        title=story.summary,
        rationale=story.description or None,
        key=story.key,
        description=story.description,
        acceptance_criteria=story.acceptance_criteria,
        priority=story.priority,
        depends_on=list(story.depends_on),
        estimated_complexity=assess_ticket_complexity(ticket).value,
    )


async def _detail(
    preview: PlanPreviewStory,
    *,
    ticket: JiraTicket,
    workspace_root: Path,
    session_id: str,
    repo_context: str,
) -> PlanPreviewStory:
    """Add TechLead analysis, or leave the story as the ScrumMaster left it.

    One story failing to plan is not a reason to lose the other four: the backlog is still
    worth reading, and the missing detail is visible as an empty ``files_to_touch`` next to
    a populated one.
    """
    try:
        task_plan, mode = await run_tech_lead(
            ticket,
            workspace_root,
            session_id=session_id,
            repo_context=repo_context,
        )
    except Exception:
        logger.exception("plan-mode TechLead failed for %s", ticket.key)
        return preview
    return _with_task_plan(preview, task_plan, mode)


def _with_task_plan(
    preview: PlanPreviewStory, task_plan: TaskPlan, planning_mode: str
) -> PlanPreviewStory:
    return preview.model_copy(
        update={
            "files_to_touch": list(task_plan.files_to_touch),
            "acceptance_tests": list(task_plan.acceptance_tests),
            "steps": [step.description for step in task_plan.steps],
            "planning_mode": planning_mode,
        }
    )


async def await_workspace_ready(session_id: str) -> Path:
    """Block until the session's checkout exists, or give up with a readable reason.

    Prep starts at session creation and clarify takes tens of seconds, so by the time
    anyone confirms and starts, the clone is almost always there. Waiting rather than
    rejecting keeps that "almost" from turning a confirmed Start into a 409 the user can
    only respond to by clicking again.
    """
    from sprint_crew.orchestrator.console_store import console_store
    from sprint_crew.schemas.console import WorkspaceStatus

    deadline = asyncio.get_running_loop().time() + get_settings().console_plan_workspace_wait_s
    while True:
        session = await asyncio.to_thread(console_store().load, session_id)
        if session is None:
            raise RuntimeError("session disappeared while waiting for its checkout")
        if session.workspace_status is WorkspaceStatus.READY and session.workspace_root:
            return Path(session.workspace_root)
        if session.workspace_status in (WorkspaceStatus.FAILED, WorkspaceStatus.EVICTED):
            raise RuntimeError(
                f"repository is {session.workspace_status.value}: "
                f"{session.workspace_error or 'no checkout to plan against'}"
            )
        if asyncio.get_running_loop().time() >= deadline:
            raise RuntimeError(
                f"repository is still {session.workspace_status.value} after "
                f"{get_settings().console_plan_workspace_wait_s:.0f}s"
            )
        await asyncio.sleep(0.2)
