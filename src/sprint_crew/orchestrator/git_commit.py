from __future__ import annotations

from pathlib import Path

from sprint_crew.git_exec import default_git_env, git_run
from sprint_crew.integrations.jira_client import redact_secrets
from sprint_crew.schemas.change import CodeChange


def branch_for_change(change: CodeChange) -> str:
    return change.branch or f"feature/{change.ticket_key.lower()}"


def commit_message_for_change(change: CodeChange) -> str:
    return f"{change.ticket_key}: {change.summary}"


def commit_change_on_branch(*, workspace: Path, change: CodeChange) -> tuple[str, str]:
    """Checkout branch, stage all changes, and commit. Returns (branch, commit_message)."""
    branch = branch_for_change(change)
    commit_msg = commit_message_for_change(change)
    env = default_git_env()
    git_run(["checkout", "-B", branch], cwd=workspace, env=env)
    git_run(["add", "-A"], cwd=workspace, env=env)
    proc = git_run(["commit", "-m", commit_msg], cwd=workspace, env=env)
    if proc.returncode != 0 and "nothing to commit" not in (proc.stdout + proc.stderr):
        raise RuntimeError(redact_secrets(proc.stderr or proc.stdout))
    return branch, commit_msg
