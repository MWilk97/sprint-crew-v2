from __future__ import annotations

import re
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
from sprint_crew.orchestrator.plan_coverage import normalize_path, step_file_paths
from sprint_crew.schemas.ticket import JiraTicket, TaskPlan
from sprint_crew.tools import READONLY_TOOLS, build_registry
from sprint_crew.tools.pydantic_ai import WorkspaceDeps, build_readonly_toolset, workspace_deps

_PATH_RE = re.compile(
    r"[\w./-]+\.(?:py|js|ts|tsx|jsx|go|rs|md|yaml|yml|json|toml|txt|sh)",
    re.IGNORECASE,
)
_MAX_FILE_SNIPPET = 4000


def _paths_from_ticket(ticket: JiraTicket) -> list[str]:
    text = "\n".join(
        [
            ticket.summary,
            ticket.description,
            ticket.acceptance_criteria,
        ]
    )
    seen: set[str] = set()
    paths: list[str] = []
    for match in _PATH_RE.findall(text):
        normalized = match.lstrip("./")
        if normalized not in seen:
            seen.add(normalized)
            paths.append(normalized)
    return paths[:8]


def _gather_repo_context(workspace_root: Path, ticket: JiraTicket | None = None) -> str:
    registry = build_registry(READONLY_TOOLS)
    parts: list[str] = []

    def run_tool(name: str, args: dict | None = None) -> str:
        result = registry.dispatch(name, args or {}, workspace_root=workspace_root)
        return result.output if result.ok else f"[{name} error] {result.output}"

    parts.append("=== git status ===")
    parts.append(run_tool("git_status"))
    parts.append("=== directory listing (.) ===")
    parts.append(run_tool("list_directory", {"path": "."}))
    parts.append("=== recent git log ===")
    parts.append(run_tool("git_log", {"n": 5}))

    if ticket is not None:
        for rel_path in _paths_from_ticket(ticket):
            parts.append(f"=== read_file: {rel_path} ===")
            content = run_tool("read_file", {"path": rel_path})
            if len(content) > _MAX_FILE_SNIPPET:
                content = content[:_MAX_FILE_SNIPPET] + "\n... (truncated)"
            parts.append(content)

        keywords = [
            word
            for word in re.findall(r"[A-Za-z_]{4,}", ticket.summary)
            if word.lower() not in {"with", "from", "that", "this"}
        ]
        if keywords:
            pattern = keywords[0]
            parts.append(f"=== grep: {pattern!r} ===")
            parts.append(run_tool("grep", {"pattern": pattern, "path": "."}))

    readme = workspace_root / "README.md"
    if readme.exists() and (ticket is None or "README.md" not in _paths_from_ticket(ticket)):
        parts.append("=== read_file: README.md ===")
        content = run_tool("read_file", {"path": "README.md"})
        if len(content) > _MAX_FILE_SNIPPET:
            content = content[:_MAX_FILE_SNIPPET] + "\n... (truncated)"
        parts.append(content)

    return "\n".join(parts)


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


async def run_tech_lead(
    ticket: JiraTicket,
    workspace_root: Path,
    *,
    session_id: str | None = None,
    prior_review_feedback: str = "",
    tool_call_log: ToolCallLog | None = None,
) -> tuple[TaskPlan, str]:
    """Run TechLead planning ladder; returns (TaskPlan, planning_mode)."""
    from sprint_crew.orchestrator.template_plan import build_template_task_plan_validated

    root = workspace_root.resolve()
    sid = session_id or root.name

    try:
        return build_template_task_plan_validated(ticket), "template"
    except RuntimeError:
        pass

    if tech_lead_mode(ticket) == "tool_loop":
        from sprint_crew.vector.indexer import should_use_vector

        log: ToolCallLog = tool_call_log if tool_call_log is not None else []
        handoff = await run_tech_lead_loop(
            ticket,
            root,
            session_id=sid,
            prior_review_feedback=prior_review_feedback,
            tool_call_log=log,
        )
        if should_use_vector(ticket=ticket) and not _tool_log_has_semantic_search(log):
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
            )
            if nudge_handoff:
                handoff = nudge_handoff
        if handoff:
            repo_context = _repo_context_for_ticket(root, ticket, session_id=sid)
            plan = _structured_plan_from_context(
                ticket,
                repo_context=repo_context,
                planning_handoff=handoff,
                prior_review_feedback=prior_review_feedback,
            )
            return plan, "tool_loop"
        repo_context = _repo_context_for_ticket(root, ticket, session_id=sid)
        plan = _structured_plan_from_context(
            ticket,
            repo_context=repo_context,
            prior_review_feedback=prior_review_feedback,
        )
        return plan, "static"

    repo_context = _repo_context_for_ticket(root, ticket, session_id=sid)
    plan = _structured_plan_from_context(
        ticket,
        repo_context=repo_context,
        prior_review_feedback=prior_review_feedback,
    )
    return plan, "static"


class PlanStructureValidationError(ValueError):
    pass


def validate_plan_structure(
    plan: TaskPlan,
    ticket: JiraTicket,
    *,
    workspace_root: Path | None = None,
    baseline_paths: frozenset[str] | None = None,
) -> None:
    """Ensure TaskPlan file lists are coherent for multi-step / non-trivial work."""
    from sprint_crew.orchestrator.plan_validation import step_requires_test_edit

    if tech_lead_mode(ticket) == "static" and len(plan.steps) <= 1:
        return

    if len(plan.steps) > 1 and not plan.files_to_touch:
        raise PlanStructureValidationError(
            "files_to_touch must be non-empty when the plan has multiple steps."
        )

    if not plan.files_to_touch:
        return

    allowed = {normalize_path(path) for path in plan.files_to_touch}
    step_paths = step_file_paths(plan)
    for step in plan.steps:
        for raw in step.files:
            normalized = normalize_path(raw)
            if normalized and normalized not in allowed:
                raise PlanStructureValidationError(
                    f"step file {raw!r} is not listed in files_to_touch."
                )

    if workspace_root is not None and baseline_paths is not None:
        for step in plan.steps:
            for raw in step.files:
                normalized = normalize_path(raw)
                if not normalized.startswith("tests/"):
                    continue
                if normalized in baseline_paths and not step_requires_test_edit(plan, normalized):
                    raise PlanStructureValidationError(
                        f"step lists existing test {raw!r} as an edit target; "
                        "use acceptance_tests only — include tests/ in steps only when "
                        "creating a new test file."
                    )

    unused = sorted(
        path for path in allowed if path not in step_paths and not path.startswith("tests/")
    )
    if unused:
        joined = ", ".join(unused)
        raise PlanStructureValidationError(
            f"files_to_touch lists paths not assigned to any step: {joined}. "
            "Remove unused paths from files_to_touch or add a step that edits them."
        )
