from __future__ import annotations

import time
from pathlib import Path

from pydantic_ai import Agent
from pydantic_ai.exceptions import ModelAPIError, UsageLimitExceeded
from pydantic_ai.models.openai import OpenAIChatModelSettings
from pydantic_ai.usage import UsageLimits

from sprint_crew.agents._factory import build_tool_agent
from sprint_crew.agents.prompts_coder import (
    build_coder_step_prompt,
    build_coder_system_prompt,
    build_coder_user_prompt,
)
from sprint_crew.config import Role, get_settings, lane_for_role
from sprint_crew.inference.router import coder_model_settings
from sprint_crew.orchestrator.run_registry import cancel_requested
from sprint_crew.orchestrator.workspace_diff import gather_workspace_diff
from sprint_crew.schemas.change import CodeChange
from sprint_crew.schemas.ticket import TaskPlan
from sprint_crew.tools.pydantic_ai import WorkspaceDeps, build_coder_toolset, workspace_deps


def _branch_name(ticket_key: str) -> str:
    return f"feature/{ticket_key.lower()}"


def normalize_change(change: CodeChange, task_plan: TaskPlan) -> CodeChange:
    updates: dict[str, object] = {}
    if not change.ticket_key:
        updates["ticket_key"] = task_plan.ticket_key
    if not change.branch:
        updates["branch"] = _branch_name(task_plan.ticket_key)
    return change.model_copy(update=updates) if updates else change


def _effective_coder_turn_limit(base: int) -> int:
    lane = lane_for_role(Role.CODING)
    return max(1, int(base * lane.request_limit_multiplier))


def _continuation_turn_budget() -> int:
    settings = get_settings()
    raw = max(12, settings.max_coder_turns // (settings.max_coverage_rounds + 1))
    return _effective_coder_turn_limit(raw)


def _tool_log_has_repeated_call(tool_log: list[dict], *, threshold: int = 3) -> bool:
    if len(tool_log) < threshold:
        return False
    recent = tool_log[-threshold:]
    first = (recent[0].get("tool"), str(recent[0].get("args")))
    return all((entry.get("tool"), str(entry.get("args"))) == first for entry in recent)


def _turn_budget_per_step(task_plan: TaskPlan) -> int:
    settings = get_settings()
    total = _effective_coder_turn_limit(settings.max_coder_turns)
    step_count = max(len(task_plan.steps), 1)
    return max(4, total // step_count)


def _build_coder_agent(
    workspace_root: Path,
    task_plan: TaskPlan,
    *,
    model_settings: OpenAIChatModelSettings | None = None,
    role_specialization: str | None = None,
    tool_call_log: list[dict] | None = None,
    baseline_paths: frozenset[str] | None = None,
) -> tuple[Agent[WorkspaceDeps, str], WorkspaceDeps]:
    if model_settings is None:
        model_settings = coder_model_settings()
    deps = workspace_deps(
        workspace_root,
        mutate=True,
        event_agent="coder",
        acceptance_tests=tuple(task_plan.acceptance_tests),
        tool_call_log=tool_call_log,
        task_plan=task_plan,
        baseline_paths=baseline_paths,
    )
    agent = build_tool_agent(
        Role.CODING,
        system_prompt=build_coder_system_prompt(role_specialization=role_specialization),
        toolset=build_coder_toolset(),
        retries=3,
        model_settings=model_settings,
    )
    return agent, deps


def _deadline_reached(deadline_epoch: float) -> bool:
    return deadline_epoch > 0.0 and time.time() >= deadline_epoch


async def run_coder_loop(
    task_plan: TaskPlan,
    workspace_root: Path,
    *,
    role_specialization: str | None = None,
    prior_review_feedback: str = "",
    user_prompt: str | None = None,
    max_turns: int | None = None,
    baseline_paths: frozenset[str] | None = None,
    deadline_epoch: float = 0.0,
    attempt: int = 0,
    tool_call_log: list[dict] | None = None,
) -> tuple[str, list[dict]]:
    """Run the Coder tool loop only; returns raw handoff text and tool-call log.

    Callers that run several loops for one graph node (step mode, continuation rounds)
    pass a shared ``tool_call_log`` so the per-call ``index`` keeps counting across loops.
    Without it each loop would restart at 1 and the live tool events (M4) would carry a
    different numbering than the node-end batch conversion over the merged log.
    """
    tool_log: list[dict] = tool_call_log if tool_call_log is not None else []
    model_settings = coder_model_settings(attempt=attempt)
    agent, deps = _build_coder_agent(
        workspace_root,
        task_plan,
        model_settings=model_settings,
        role_specialization=role_specialization,
        tool_call_log=tool_log,
        baseline_paths=baseline_paths,
    )
    plan_json = task_plan.model_dump_json(indent=2)
    prompt = user_prompt or build_coder_user_prompt(
        plan_json, prior_review_feedback=prior_review_feedback
    )
    turn_limit = (
        max_turns
        if max_turns is not None
        else _effective_coder_turn_limit(get_settings().max_coder_turns)
    )
    usage_limits = UsageLimits(request_limit=turn_limit)

    try:
        async with agent.iter(
            prompt,
            deps=deps,
            usage_limits=usage_limits,
            model_settings=model_settings,
        ) as run:
            async for _ in run:
                if deps.early_exit_handoff:
                    break
                if tool_log and _tool_log_has_repeated_call(tool_log):
                    break
                if _deadline_reached(deadline_epoch):
                    break
                # Break rather than raise: edits are already on disk, so hand off the
                # partial work and let the graph's node-entry checkpoint end the run.
                if cancel_requested():
                    break
    except UsageLimitExceeded:
        if deps.early_exit_handoff:
            return deps.early_exit_handoff, tool_log
        return "Continuation turn budget exhausted; handing off partial work.", tool_log
    except ModelAPIError as exc:
        # A mid-loop model error (context-window overflow, request timeout) must
        # not crash the whole cycle: partial edits are already on disk, so hand
        # off and let review/retry gate the result.
        if deps.early_exit_handoff:
            return deps.early_exit_handoff, tool_log
        return (
            f"Coder model request failed mid-loop ({exc}); handing off partial work.",
            tool_log,
        )

    if deps.early_exit_handoff:
        return deps.early_exit_handoff, tool_log
    if run.result is not None and run.result.output:
        return run.result.output, tool_log
    return "Coder loop ended without structured handoff.", tool_log


async def run_coder_plan(
    task_plan: TaskPlan,
    workspace_root: Path,
    *,
    role_specialization: str | None = None,
    prior_review_feedback: str = "",
    baseline_paths: frozenset[str] | None = None,
    deadline_epoch: float = 0.0,
    attempt: int = 0,
    tool_call_log: list[dict] | None = None,
) -> tuple[str, list[dict]]:
    """Run Coder across TaskPlan steps (fresh session per step when step mode enabled)."""
    settings = get_settings()
    plan_json = task_plan.model_dump_json(indent=2)

    if not settings.coder_step_mode or len(task_plan.steps) <= 1:
        return await run_coder_loop(
            task_plan,
            workspace_root,
            role_specialization=role_specialization,
            prior_review_feedback=prior_review_feedback,
            baseline_paths=baseline_paths,
            deadline_epoch=deadline_epoch,
            attempt=attempt,
            tool_call_log=tool_call_log,
        )

    all_logs: list[dict] = tool_call_log if tool_call_log is not None else []
    raw_output = ""
    turn_budget = _turn_budget_per_step(task_plan)
    total_steps = len(task_plan.steps)
    remaining_turns = _effective_coder_turn_limit(settings.max_coder_turns)

    for idx, step in enumerate(task_plan.steps):
        if _deadline_reached(deadline_epoch) or cancel_requested():
            break
        step_turns = min(turn_budget, remaining_turns)
        step_prompt = build_coder_step_prompt(
            task_plan_json=plan_json,
            step_index=idx,
            step=step,
            total_steps=total_steps,
            workspace_diff=gather_workspace_diff(workspace_root, max_chars=8000),
            prior_review_feedback=prior_review_feedback,
        )
        raw_output, _ = await run_coder_loop(
            task_plan,
            workspace_root,
            role_specialization=role_specialization,
            user_prompt=step_prompt,
            max_turns=step_turns,
            baseline_paths=baseline_paths,
            deadline_epoch=deadline_epoch,
            attempt=attempt,
            tool_call_log=all_logs,
        )
        remaining_turns = max(0, remaining_turns - step_turns)

    return raw_output, all_logs
