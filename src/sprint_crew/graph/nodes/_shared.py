"""Lane and diff helpers shared by the node modules."""

from __future__ import annotations

from pathlib import Path

from sprint_crew.config import Role
from sprint_crew.graph import lanes
from sprint_crew.graph.pipeline_helpers import (
    _in_backlog_batch,
)
from sprint_crew.graph.state import (
    SprintState,
)
from sprint_crew.orchestrator import workspace_diff as diff_tools
from sprint_crew.schemas.ticket import TaskPlan


async def _stop_lane_after_cycle(state: SprintState, role: Role) -> None:
    if _in_backlog_batch(state):
        return
    await lanes.stop_lane(role)


async def _swap_lane(stop: Role, start: Role) -> None:
    """Only one lane may be loaded at a time on 128 GB unified memory (AGENTS.md 4.1),
    so every lane change is a stop followed by a start, never a start alone."""
    await lanes.stop_lane(stop)
    await lanes.ensure_lane(start)


def _diff_for(state: SprintState, workspace: Path, plan: TaskPlan) -> str:
    """Reuse the diff already gathered this cycle, or compute it. Recomputing costs a git
    subprocess per node, which is why the state value is preferred when present."""
    return state.get("workspace_diff") or diff_tools.gather_workspace_diff(
        workspace, priority_paths=plan.files_to_touch
    )
