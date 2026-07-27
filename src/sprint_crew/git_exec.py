"""Orchestrator-side git subprocess wrapper.

Lives at package root rather than under ``integrations/`` (its previous home, alongside the
Jira client) because four orchestrator modules depend on it and none of them have anything
to do with Jira. Distinct from ``tools/_git_utils.py``, which is the sandboxed, agent-facing
reader that has to cope with a split ``GIT_DIR``/``GIT_WORK_TREE`` worktree; this one runs
plain commands in a workspace the orchestrator owns.
"""

from __future__ import annotations

import logging
import os
import subprocess
from pathlib import Path

from sprint_crew.config import get_settings

logger = logging.getLogger(__name__)


def git_run(
    argv: list[str], *, cwd: Path, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    # timeout guards against a hung git (e.g. a network push) blocking the event loop
    # forever; TimeoutExpired propagates so the caller fails loudly rather than silently.
    return subprocess.run(
        ["git", *argv],
        cwd=cwd,
        capture_output=True,
        text=True,
        env=env or os.environ.copy(),
        check=False,
        timeout=get_settings().git_timeout_s,
    )


def git_output(argv: list[str], cwd: Path) -> str:
    """Combined stdout+stderr, for read-only queries whose exit code the caller ignores.

    Callers that inspect status/diff text want one string and treat a failure as "no
    output"; keeping that shape here is what let a second, timeout-less copy of git_run
    grow in plan_coverage.py.

    A timeout is one of those failures. ``git_run`` lets ``TimeoutExpired`` propagate so a
    caller that checks exit codes fails loudly, but this one promises a string — and it is
    called from the coverage hot path inside a tool call, where raising would abort the
    agent's turn over a slow ``git status``.
    """
    try:
        proc = git_run(argv, cwd=cwd)
    except subprocess.TimeoutExpired:
        logger.warning("git %s timed out in %s; treating as no output", argv[0], cwd)
        return ""
    return (proc.stdout or "") + (proc.stderr or "")


def default_git_env() -> dict[str, str]:
    return {
        **os.environ,
        "GIT_AUTHOR_NAME": "sprint-crew",
        "GIT_AUTHOR_EMAIL": "sprint-crew@local",
        "GIT_COMMITTER_NAME": "sprint-crew",
        "GIT_COMMITTER_EMAIL": "sprint-crew@local",
    }
