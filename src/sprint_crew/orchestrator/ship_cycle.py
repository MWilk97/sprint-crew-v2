from __future__ import annotations

from typing import Any

from sprint_crew.graph.state import SprintState, code_change_from_state, workspace_from_state
from sprint_crew.orchestrator.git_commit import commit_change_on_branch
from sprint_crew.schemas.session import SessionStatus
from sprint_crew.schemas.session import agent_event as _event


async def orchestrator_ship(state: SprintState) -> dict[str, Any]:
    if state.get("use_real_ship"):
        from sprint_crew.orchestrator.sprint import ship_from_graph_state

        return await ship_from_graph_state(state)
    return await ship_stub(state)


async def ship_stub(state: SprintState) -> dict[str, Any]:
    workspace = workspace_from_state(state)
    change = code_change_from_state(state)
    branch, commit_msg = commit_change_on_branch(workspace=workspace, change=change)
    return {
        "branch": branch,
        "status": SessionStatus.AWAITING_HUMAN,
        "events": [
            _event(
                "orchestrator",
                "shipped_stub",
                f"Local commit on {branch} (no push)",
                commit_message=commit_msg,
            ),
        ],
    }
