from __future__ import annotations

import asyncio
from pathlib import Path

from pydantic_ai import Agent
from pydantic_ai.exceptions import UsageLimitExceeded
from pydantic_ai.settings import ModelSettings
from pydantic_ai.usage import UsageLimits

from sprint_crew.agents.prompts_tester import (
    build_tester_reporter_user_prompt,
    build_tester_system_prompt,
    build_tester_user_prompt,
)
from sprint_crew.config import Role, get_settings, lane_for_role
from sprint_crew.inference.router import pydantic_ai_model
from sprint_crew.inference.structured import structured_completion
from sprint_crew.schemas.change import CodeChange, TestAdditions
from sprint_crew.schemas.ticket import TaskPlan
from sprint_crew.tools.pydantic_ai import WorkspaceDeps, build_tester_toolset, workspace_deps


def _effective_tester_turn_limit() -> int:
    settings = get_settings()
    lane = lane_for_role(Role.CODING)
    return max(1, int(settings.max_tester_turns * lane.request_limit_multiplier))


async def run_tester_loop(
    task_plan: TaskPlan,
    code_change: CodeChange,
    workspace_root: Path,
    *,
    acceptance_green: bool = False,
    acceptance_output: str = "",
) -> tuple[str, list[dict]]:
    tool_log: list[dict] = []
    deps = workspace_deps(workspace_root, mutate=True, event_agent="tester", tool_call_log=tool_log)
    agent: Agent[WorkspaceDeps, str] = Agent(
        pydantic_ai_model(Role.CODING),
        deps_type=WorkspaceDeps,
        system_prompt=build_tester_system_prompt(),
        toolsets=[build_tester_toolset()],
        retries=1,
        model_settings=ModelSettings(temperature=0),
    )
    plan_json = task_plan.model_dump_json(indent=2)
    change_json = code_change.model_dump_json(indent=2)
    try:
        result = await agent.run(
            build_tester_user_prompt(
                task_plan_json=plan_json,
                code_change_json=change_json,
                acceptance_green=acceptance_green,
                acceptance_output=acceptance_output,
            ),
            deps=deps,
            usage_limits=UsageLimits(request_limit=_effective_tester_turn_limit()),
            model_settings=ModelSettings(temperature=0),
        )
    except UsageLimitExceeded:
        return "Tester turn budget exhausted; handing off partial test work.", tool_log
    return result.output, tool_log


async def run_tester_reporter(task_plan: TaskPlan, raw_output: str) -> TestAdditions:
    plan_json = task_plan.model_dump_json(indent=2)
    additions = await asyncio.to_thread(
        structured_completion,
        Role.WORK,
        system_prompt="Convert tester handoff to TestAdditions JSON.",
        user_prompt=build_tester_reporter_user_prompt(
            task_plan_json=plan_json,
            raw_output=raw_output,
        ),
        output_type=TestAdditions,
    )
    if not additions.ticket_key:
        additions = additions.model_copy(update={"ticket_key": task_plan.ticket_key})
    return additions
