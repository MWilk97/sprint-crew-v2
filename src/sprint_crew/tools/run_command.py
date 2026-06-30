from __future__ import annotations

import os
import shlex
import shutil
import subprocess
from pathlib import Path

from pydantic import BaseModel, Field

from sprint_crew.tools.base import ToolError, ToolResult

ALLOWED_COMMANDS: frozenset[str] = frozenset(
    {
        "pytest",
        "python",
        "python3",
        "npm",
        "node",
        "ruff",
        "mypy",
        "pip",
        "uv",
    }
)

_ENV_PASSTHROUGH: frozenset[str] = frozenset(
    {
        "PATH",
        "HOME",
        "LANG",
        "LC_ALL",
        "VIRTUAL_ENV",
        "PYTHONPATH",
        "UV_PROJECT_ENVIRONMENT",
    }
)

_DEFAULT_TIMEOUT_SECONDS = 300


class RunCommandArgs(BaseModel):
    command: str = Field(..., min_length=1)


def _resolve_executable(argv0: str) -> str:
    if os.path.sep in argv0 or (os.path.altsep and os.path.altsep in argv0):
        raise ToolError(f"Executable must be a bare command name, got {argv0!r}.")
    resolved = shutil.which(argv0)
    if resolved is None:
        raise ToolError(f"Command not found on PATH: {argv0!r}.")
    return resolved


def _harden_argv(argv: list[str]) -> list[str]:
    if not argv:
        raise ToolError("Empty command.")
    if argv[0] not in ALLOWED_COMMANDS:
        allowed = ", ".join(sorted(ALLOWED_COMMANDS))
        raise ToolError(f"Command {argv[0]!r} not allowlisted. Allowed: {allowed}.")
    argv[0] = _resolve_executable(argv[0])
    if argv[0].endswith("npm") or Path(argv[0]).name == "npm":
        if "--ignore-scripts" not in argv:
            argv = [*argv, "--ignore-scripts"]
    return argv


class RunCommandTool:
    name = "run_command"
    description = "Run an allowlisted shell command in the workspace root."
    args_schema = RunCommandArgs

    def execute(self, args: BaseModel, *, workspace_root: Path) -> ToolResult:
        assert isinstance(args, RunCommandArgs)
        try:
            argv = shlex.split(args.command)
            argv = _harden_argv(argv)
        except ToolError as exc:
            return ToolResult(ok=False, output=str(exc), error="not allowed")

        env = {k: v for k, v in os.environ.items() if k in _ENV_PASSTHROUGH}
        try:
            proc = subprocess.run(
                argv,
                cwd=str(workspace_root.resolve(strict=False)),
                env=env,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=_DEFAULT_TIMEOUT_SECONDS,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return ToolResult(ok=False, output=f"Command timed out after {_DEFAULT_TIMEOUT_SECONDS}s.", error="timeout")

        combined = (proc.stdout or "") + (proc.stderr or "")
        ok = proc.returncode == 0
        return ToolResult(
            ok=ok,
            output=combined or f"(exit {proc.returncode})",
            data={"returncode": proc.returncode, "argv": argv},
            error=None if ok else f"exit {proc.returncode}",
        )


run_command_tool = RunCommandTool()
