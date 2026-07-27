"""The orchestrator-side git wrapper: one timeout policy, two error models."""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from sprint_crew.git_exec import default_git_env, git_output, git_run


def test_git_run_applies_the_configured_timeout(tmp_path: Path) -> None:
    with patch("sprint_crew.git_exec.subprocess.run") as run:
        run.return_value = subprocess.CompletedProcess([], 0, "", "")
        git_run(["status"], cwd=tmp_path)

    assert run.call_args.kwargs["timeout"] > 0


def test_git_run_propagates_a_timeout(tmp_path: Path) -> None:
    """Callers that inspect exit codes must fail loudly rather than see a blank result."""
    with patch(
        "sprint_crew.git_exec.subprocess.run",
        side_effect=subprocess.TimeoutExpired(cmd=["git"], timeout=1),
    ):
        with pytest.raises(subprocess.TimeoutExpired):
            git_run(["push"], cwd=tmp_path)


def test_git_output_treats_a_timeout_as_no_output(tmp_path: Path) -> None:
    """git_output promises a string to callers that ignore exit codes — and it is called
    from the coverage hot path inside a tool call, where raising would abort the agent's
    turn over a slow `git status`."""
    with patch(
        "sprint_crew.git_exec.subprocess.run",
        side_effect=subprocess.TimeoutExpired(cmd=["git"], timeout=1),
    ):
        assert git_output(["status", "--porcelain"], tmp_path) == ""


def test_git_output_combines_stdout_and_stderr(tmp_path: Path) -> None:
    with patch("sprint_crew.git_exec.subprocess.run") as run:
        run.return_value = subprocess.CompletedProcess([], 1, "out\n", "err\n")
        assert git_output(["status"], tmp_path) == "out\nerr\n"


def test_default_git_env_pins_an_identity() -> None:
    env = default_git_env()
    assert env["GIT_AUTHOR_NAME"] == "sprint-crew"
    assert env["GIT_COMMITTER_EMAIL"] == "sprint-crew@local"
