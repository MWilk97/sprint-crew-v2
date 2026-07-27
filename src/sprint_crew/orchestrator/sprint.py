from __future__ import annotations

from pathlib import Path
from typing import Any

from sprint_crew.config import get_settings
from sprint_crew.git_exec import default_git_env, git_run
from sprint_crew.graph.state import SprintState
from sprint_crew.integrations.jira_client import (
    get_github_client,
    get_jira_client,
    origin_repo_slug,
    redact_secrets,
)
from sprint_crew.orchestrator.git_commit import commit_change_on_branch
from sprint_crew.orchestrator.session import get_session
from sprint_crew.schemas.change import CodeChange
from sprint_crew.schemas.session import AgentEvent, SessionStatus, SprintSession


async def ship(session: SprintSession) -> SprintSession:
    if session.code_change is None:
        raise ValueError("Session has no code_change to ship")
    workspace = Path(session.workspace_root)
    change = session.code_change
    branch, commit_msg = commit_change_on_branch(workspace=workspace, change=change)
    env = default_git_env()

    push = git_run(["push", "-u", "origin", branch], cwd=workspace, env=env)
    if push.returncode != 0:
        raise RuntimeError(redact_secrets(push.stderr or push.stdout))

    repo_slug = origin_repo_slug(workspace)
    github = get_github_client(repo_slug=repo_slug)
    pr_body = change.notes or change.summary
    pr_url = github.create_pull_request(
        title=commit_msg,
        body=pr_body,
        head=branch,
    )

    jira = get_jira_client()
    jira.add_comment(session.ticket_key, f"PR opened: {pr_url}")
    jira.transition(session.ticket_key, get_settings().jira_review_transition)

    updated = session.model_copy(
        update={
            "status": SessionStatus.AWAITING_HUMAN,
            "branch": branch,
            "pr_url": pr_url,
            "events": [
                *session.events,
                AgentEvent(
                    agent="orchestrator",
                    event_type="shipped",
                    summary=f"Pushed {branch} and opened PR",
                    detail={"pr_url": pr_url, "branch": branch},
                ),
            ],
        }
    )
    from sprint_crew.orchestrator.session import session_store

    session_store().save(updated)
    return updated


async def ship_from_graph_state(state: SprintState) -> dict[str, Any]:
    session = get_session(state["session_id"])
    if session is None:
        session = SprintSession(
            session_id=state["session_id"],
            status=SessionStatus.RUNNING,
            ticket_key=CodeChange.model_validate(state["code_change"]).ticket_key,
            workspace_root=state["workspace_root"],
            code_change=CodeChange.model_validate(state["code_change"]),
        )
    else:
        session = session.model_copy(
            update={"code_change": CodeChange.model_validate(state["code_change"])}
        )
    shipped = await ship(session)
    return {
        "branch": shipped.branch,
        "pr_url": shipped.pr_url,
        "status": shipped.status,
        "events": [
            AgentEvent(
                agent="orchestrator",
                event_type="shipped",
                summary=f"Shipped to {shipped.pr_url or shipped.branch}",
            ),
        ],
    }
