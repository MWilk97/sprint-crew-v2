from __future__ import annotations

from sprint_crew.agents.prompts_scrum_master import (
    build_scrum_master_system_prompt,
    build_scrum_master_user_prompt,
)
from sprint_crew.config import Role
from sprint_crew.inference.structured import structured_completion
from sprint_crew.orchestrator.backlog import normalize_backlog_plan
from sprint_crew.schemas.backlog import BacklogPlan


async def run_scrum_master(
    *,
    user_prompt: str,
    repo_context: str = "",
    role: Role | None = None,
) -> BacklogPlan:
    lane = role or Role.WORK
    plan = structured_completion(
        lane,
        system_prompt=build_scrum_master_system_prompt(),
        user_prompt=build_scrum_master_user_prompt(
            user_prompt=user_prompt,
            repo_context=repo_context,
        ),
        output_type=BacklogPlan,
    )
    return normalize_backlog_plan(plan, user_prompt=user_prompt)
