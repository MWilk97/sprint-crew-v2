from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from sprint_crew.tools.base import ToolError
from sprint_crew.tools.run_command import (
    RunCommandArgs,
    RunCommandTool,
    _harden_argv,
    _resolve_executable,
)


def test_harden_argv_accepts_pytest() -> None:
    argv = _harden_argv(["pytest", "-q"])
    assert Path(argv[0]).name in {"pytest", "pytest.exe"}


def test_harden_argv_rejects_curl() -> None:
    with pytest.raises(ToolError, match="not allowlisted"):
        _harden_argv(["curl", "http://localhost"])


def test_harden_argv_rejects_bash() -> None:
    with pytest.raises(ToolError, match="not allowlisted"):
        _harden_argv(["bash", "-c", "echo hi"])


def test_harden_argv_rejects_absolute_not_on_allowlist() -> None:
    with pytest.raises(ToolError, match="not allowlisted"):
        _harden_argv(["/usr/bin/pytest", "-q"])


def test_resolve_executable_rejects_bare_path() -> None:
    with pytest.raises(ToolError, match="bare command name"):
        _resolve_executable("./pytest")


def test_harden_argv_injects_npm_ignore_scripts(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "sprint_crew.tools.run_command._resolve_executable",
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


def test_run_command_timeout_returns_error(tmp_path: Path) -> None:
    tool = RunCommandTool()
    with patch(
        "sprint_crew.tools.run_command.subprocess.run",
        side_effect=subprocess.TimeoutExpired(cmd=["pytest"], timeout=300),
    ):
        result = tool.execute(RunCommandArgs(command="pytest -q"), workspace_root=tmp_path)
    assert result.ok is False
    assert result.error == "timeout"
