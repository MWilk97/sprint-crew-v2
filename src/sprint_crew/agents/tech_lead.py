from __future__ import annotations

from pathlib import Path

from pydantic_ai import Agent
from pydantic_ai.settings import ModelSettings
from pydantic_ai.usage import UsageLimits

from sprint_crew.agents.prompts_tech_lead import (
    build_tech_lead_loop_user_prompt,
    build_tech_lead_structured_prompt,
    build_tech_lead_system_prompt,
    build_tech_lead_user_prompt,
)
from sprint_crew.agents.tool_events import ToolCallLog
from sprint_crew.config import Role, get_settings
from sprint_crew.inference.router import pydantic_ai_model
from sprint_crew.inference.structured import structured_completion
from sprint_crew.orchestrator.complexity import tech_lead_mode
from sprint_crew.orchestrator.repo_context import gather_repo_context, paths_from_ticket
from sprint_crew.schemas.ticket import JiraTicket, TaskPlan
from sprint_crew.tools.pydantic_ai import WorkspaceDeps, build_readonly_toolset, workspace_deps


def _paths_from_ticket(ticket: JiraTicket) -> list[str]:
    return paths_from_ticket(ticket)


def _gather_repo_context(workspace_root: Path, ticket: JiraTicket | None = None) -> str:
    return gather_repo_context(workspace_root, ticket)


def _repo_context_for_ticket(
    workspace_root: Path,
    ticket: JiraTicket,
    *,
    session_id: str | None = None,
) -> str:
    from sprint_crew.vector.context import enrich_repo_context
    from sprint_crew.vector.indexer import should_use_vector

    root = workspace_root.resolve()
    if not should_use_vector(ticket=ticket):
        return _gather_repo_context(root, ticket)

    sid = session_id or root.name
    query = f"{ticket.summary}\n{ticket.description}"
    return enrich_repo_context(root, sid, query, ticket=ticket)


async def run_tech_lead_loop(
    ticket: JiraTicket,
    workspace_root: Path,
    *,
    session_id: str | None = None,
    prior_review_feedback: str = "",
    tool_call_log: ToolCallLog | None = None,
    repo_context: str | None = None,
) -> str:
    """Explore the repo with read-only tools; return a plain-text planning handoff."""
    root = workspace_root.resolve()
    sid = session_id or root.name
    log: ToolCallLog = tool_call_log if tool_call_log is not None else []
    deps = workspace_deps(
        root,
        mutate=False,
        session_id=sid,
        include_semantic_search=True,
        tool_call_log=log,
    )
    agent: Agent[WorkspaceDeps, str] = Agent(
        pydantic_ai_model(Role.WORK),
        deps_type=WorkspaceDeps,
        system_prompt=build_tech_lead_system_prompt(),
        toolsets=[build_readonly_toolset(include_semantic_search=True)],
        retries=3,
        model_settings=ModelSettings(temperature=0),
    )
    if repo_context is None:
        repo_context = _repo_context_for_ticket(root, ticket, session_id=sid)
    prompt = build_tech_lead_loop_user_prompt(
        ticket_json=ticket.model_dump_json(indent=2),
        repo_context=repo_context,
        prior_review_feedback=prior_review_feedback,
    )
    result = await agent.run(
        prompt,
        deps=deps,
        usage_limits=UsageLimits(request_limit=get_settings().max_techlead_turns),
        model_settings=ModelSettings(temperature=0),
    )
    return result.output.strip()


def _structured_plan_from_context(
    ticket: JiraTicket,
    *,
    repo_context: str,
    planning_handoff: str = "",
    prior_review_feedback: str = "",
) -> TaskPlan:
    if planning_handoff.strip():
        user_prompt = build_tech_lead_structured_prompt(
            ticket_json=ticket.model_dump_json(indent=2),
            repo_context=repo_context,
            planning_handoff=planning_handoff,
            prior_review_feedback=prior_review_feedback,
        )
    else:
        user_prompt = build_tech_lead_user_prompt(
            ticket_json=ticket.model_dump_json(indent=2),
            repo_context=repo_context,
            prior_review_feedback=prior_review_feedback,
        )
    return structured_completion(
        Role.WORK,
        system_prompt=build_tech_lead_system_prompt(),
        user_prompt=user_prompt,
        output_type=TaskPlan,
    )


def _tool_log_has_semantic_search(tool_log: ToolCallLog) -> bool:
    return any(entry.get("tool") == "semantic_search" for entry in tool_log)


def _semantic_retrieval_satisfied(
    tool_log: ToolCallLog,
    *,
    pre_search_hits: int = 0,
) -> bool:
    if pre_search_hits > 0:
        return True
    return _tool_log_has_semantic_search(tool_log)


def _append_programmatic_semantic_search(
    handoff: str,
    *,
    session_id: str,
    ticket: JiraTicket,
    tool_log: ToolCallLog,
) -> str:
    from sprint_crew.vector.search import format_search_hits, semantic_search

    query = f"{ticket.summary}\n{ticket.description}"
    hits = semantic_search(session_id, query)
    body = format_search_hits(hits)
    tool_log.append(
        {
            "tool": "semantic_search",
            "args": {"query": query[:500]},
            "ok": True,
            "output_preview": body if len(body) <= 500 else body[:500] + "…",
        }
    )
    if not body.strip() or body == "(no semantic matches)":
        return handoff
    prefix = "=== programmatic semantic_search ===\n"
    if handoff.strip():
        return f"{handoff.strip()}\n\n{prefix}{body}"
    return f"{prefix}{body}"


async def run_tech_lead(
    ticket: JiraTicket,
    workspace_root: Path,
    *,
    session_id: str | None = None,
    prior_review_feedback: str = "",
    tool_call_log: ToolCallLog | None = None,
    pre_search_hit_count: int = 0,
    repo_context: str | None = None,
) -> tuple[TaskPlan, str]:
    """Run TechLead planning ladder; returns (TaskPlan, planning_mode).

    ``repo_context`` lets the orchestrator pass in an already-gathered context
    (the graph node computes it for pre_search) so the ladder doesn't re-run the
    manifest build + semantic_search for every planning step.
    """
    from sprint_crew.orchestrator.template_plan import build_template_task_plan_validated

    root = workspace_root.resolve()
    sid = session_id or root.name

    try:
        return build_template_task_plan_validated(ticket), "template"
    except RuntimeError:
        pass

    # Repo context is static for this planning attempt — gather it once (or reuse the
    # orchestrator-provided context) and share it across the tool loop and structured plan.
    context = (
        repo_context
        if repo_context is not None
        else _repo_context_for_ticket(root, ticket, session_id=sid)
    )

    if tech_lead_mode(ticket) == "tool_loop":
        from sprint_crew.vector.indexer import should_use_vector

        log: ToolCallLog = tool_call_log if tool_call_log is not None else []
        handoff = await run_tech_lead_loop(
            ticket,
            root,
            session_id=sid,
            prior_review_feedback=prior_review_feedback,
            tool_call_log=log,
            repo_context=context,
        )
        if should_use_vector(ticket=ticket) and not _semantic_retrieval_satisfied(
            log, pre_search_hits=pre_search_hit_count
        ):
            nudge_feedback = (
                f"{prior_review_feedback}\n\n".strip()
                + "You must call semantic_search at least once when the repo is indexed. "
                "Call it now, verify hits with read_file/grep, then update your handoff."
            ).strip()
            nudge_handoff = await run_tech_lead_loop(
                ticket,
                root,
                session_id=sid,
                prior_review_feedback=nudge_feedback,
                tool_call_log=log,
                repo_context=context,
            )
            if nudge_handoff:
                handoff = nudge_handoff
        if should_use_vector(ticket=ticket) and not _semantic_retrieval_satisfied(
            log, pre_search_hits=pre_search_hit_count
        ):
            handoff = _append_programmatic_semantic_search(
                handoff,
                session_id=sid,
                ticket=ticket,
                tool_log=log,
            )
        if handoff:
            plan = _structured_plan_from_context(
                ticket,
                repo_context=context,
                planning_handoff=handoff,
                prior_review_feedback=prior_review_feedback,
            )
            return plan, "tool_loop"
        plan = _structured_plan_from_context(
            ticket,
            repo_context=context,
            prior_review_feedback=prior_review_feedback,
        )
        return plan, "static"

    plan = _structured_plan_from_context(
        ticket,
        repo_context=context,
        prior_review_feedback=prior_review_feedback,
    )
    return plan, "static"
