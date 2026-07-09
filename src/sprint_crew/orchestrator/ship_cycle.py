from __future__ import annotations

from pathlib import Path
from typing import Any

from sprint_crew.graph.state import SprintState
from sprint_crew.orchestrator.git_commit import commit_change_on_branch
from sprint_crew.schemas.change import CodeChange
from sprint_crew.schemas.session import AgentEvent, SessionStatus


def _event(agent: str, event_type: str, summary: str, **detail: Any) -> AgentEvent:
    payload = detail or None
    return AgentEvent(agent=agent, event_type=event_type, summary=summary, detail=payload)


def _workspace(state: SprintState) -> Path:
    return Path(state["workspace_root"])


def _code_change(state: SprintState) -> CodeChange:
    return CodeChange.model_validate(state["code_change"])


async def orchestrator_ship(state: SprintState) -> dict[str, Any]:
    if state.get("use_real_ship"):
        from sprint_crew.orchestrator.sprint import ship_from_graph_state

        return await ship_from_graph_state(state)
    return await ship_stub(state)


async def ship_stub(state: SprintState) -> dict[str, Any]:
    workspace = _workspace(state)
    change = _code_change(state)
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
