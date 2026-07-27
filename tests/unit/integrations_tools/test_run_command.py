from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest
from tests.helpers.child_processes import write_grandchild_spawner

from sprint_crew.exec_policy import (
    CommandPolicyError,
    check_command_allowed,
    resolve_executable,
    sandbox_env,
)
from sprint_crew.tools.run_command import (
    RunCommandArgs,
    RunCommandTool,
    _harden_argv,
)


def test_harden_argv_accepts_pytest() -> None:
    argv = _harden_argv(["pytest", "-q"])
    assert Path(argv[0]).name in {"pytest", "pytest.exe"}


def test_harden_argv_rejects_curl() -> None:
    with pytest.raises(CommandPolicyError, match="not allowlisted"):
        _harden_argv(["curl", "http://localhost"])


def test_harden_argv_rejects_bash() -> None:
    with pytest.raises(CommandPolicyError, match="not allowlisted"):
        _harden_argv(["bash", "-c", "echo hi"])


def test_harden_argv_rejects_absolute_not_on_allowlist() -> None:
    with pytest.raises(CommandPolicyError, match="not allowlisted"):
        _harden_argv(["/usr/bin/pytest", "-q"])


def test_resolve_executable_rejects_bare_path() -> None:
    with pytest.raises(CommandPolicyError, match="bare command name"):
        resolve_executable("./pytest")


def test_harden_argv_injects_npm_ignore_scripts(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "sprint_crew.tools.run_command.resolve_executable",
        lambda name: "/usr/bin/npm",
    )
    argv = _harden_argv(["npm", "install"])
    assert "--ignore-scripts" in argv


def test_run_command_accepts_pytest_version(tmp_path: Path) -> None:
    tool = RunCommandTool()
    result = tool.execute(RunCommandArgs(command="pytest --version"), workspace_root=tmp_path)
    assert result.ok is True


def test_run_command_rejects_curl(tmp_path: Path) -> None:
    tool = RunCommandTool()
    result = tool.execute(RunCommandArgs(command="curl http://evil"), workspace_root=tmp_path)
    assert result.ok is False
    assert "not allowlisted" in result.output


@pytest.mark.parametrize(
    "command",
    [
        "pytest -q; env",
        "pytest -q && curl http://evil",
        "pytest -q | tee /tmp/out",
        "pytest -q $(env)",
        "pytest -q > /tmp/out",
    ],
)
def test_run_command_rejects_shell_syntax(tmp_path: Path, command: str) -> None:
    """argv[0] is `pytest` in every one of these. Checking only argv[0] let them through."""
    tool = RunCommandTool()
    result = tool.execute(RunCommandArgs(command=command), workspace_root=tmp_path)
    assert result.ok is False
    assert "shell operators" in result.output


def test_run_command_rejects_backtick_substitution(tmp_path: Path) -> None:
    tool = RunCommandTool()
    result = tool.execute(RunCommandArgs(command="pytest -q `env`"), workspace_root=tmp_path)
    assert result.ok is False
    assert "backtick" in result.output


@pytest.mark.parametrize(
    "command",
    [
        'python -c "import os; print(os.environ)"',
        'pytest -q -k "a or b"',
        "pytest tests/unit -q",
    ],
)
def test_quoted_operators_are_not_mistaken_for_shell_syntax(command: str) -> None:
    """Precision matters: a substring scan would reject all three of these."""
    assert check_command_allowed(command) is None


def test_run_command_timeout_returns_error(tmp_path: Path) -> None:
    tool = RunCommandTool()
    with patch(
        "sprint_crew.tools.run_command.subprocess.run",
        side_effect=subprocess.TimeoutExpired(cmd=["pytest"], timeout=300),
    ):
        result = tool.execute(RunCommandArgs(command="pytest -q"), workspace_root=tmp_path)
    assert result.ok is False
    assert result.error == "timeout"


def test_sandbox_env_drops_secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HF_TOKEN", "hf-secret")
    monkeypatch.setenv("JIRA_API_TOKEN", "jira-secret")
    monkeypatch.setenv("CONSOLE_API_TOKEN", "console-secret")
    monkeypatch.setenv("PATH", "/usr/bin")

    env = sandbox_env()

    assert env["PATH"] == "/usr/bin"
    assert "HF_TOKEN" not in env
    assert "JIRA_API_TOKEN" not in env
    assert "CONSOLE_API_TOKEN" not in env


@pytest.mark.asyncio
async def test_run_command_child_does_not_see_secrets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End to end: the token in this process must not reach the model's subprocess."""
    monkeypatch.setenv("HF_TOKEN", "hf-secret")
    tool = RunCommandTool()

    result = await tool.aexecute(
        RunCommandArgs(command="python -c \"import os; print(os.environ.get('HF_TOKEN'))\""),
        workspace_root=tmp_path,
    )

    assert result.ok is True
    assert "hf-secret" not in result.output
    assert "None" in result.output


@pytest.mark.asyncio
async def test_cancelling_run_command_kills_the_child(tmp_path: Path) -> None:
    """The gap the branch left open: acceptance tests became killable, the tool did not.

    A `to_thread(subprocess.run)` dispatch unwinds the coroutine and leaves the child
    running for up to the 300 s cap — holding the single run slot against the next run.

    The command spawns a *grandchild* that writes the marker, so signalling only the
    direct pid would leave it alive and the marker would still appear.
    """
    marker = tmp_path / "child-survived"
    child_lifetime = 2.0
    script = write_grandchild_spawner(tmp_path / "spawn.py", lifetime_s=child_lifetime)

    tool = RunCommandTool()
    task = asyncio.create_task(
        tool.aexecute(RunCommandArgs(command=f"python {script} {marker}"), workspace_root=tmp_path)
    )
    await asyncio.sleep(0.4)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    # Must outlast the grandchild, or the assertion is vacuous: an orphan that has not yet
    # reached its `touch` looks identical to one that was killed.
    await asyncio.sleep(child_lifetime)
    assert not marker.exists(), "run_command child outlived the cancelled run"
