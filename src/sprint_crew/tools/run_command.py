from __future__ import annotations

import shlex
import subprocess
from pathlib import Path

from pydantic import BaseModel, Field

from sprint_crew.exec_policy import (
    ALLOWED_COMMANDS,
    CommandPolicyError,
    check_argv_allowlisted,
    check_command_allowed,
    resolve_executable,
    sandbox_env,
)
from sprint_crew.proc import run_argv
from sprint_crew.tools.base import ToolError, ToolResult

__all__ = ["ALLOWED_COMMANDS", "RunCommandArgs", "RunCommandTool", "run_command_tool"]

_DEFAULT_TIMEOUT_SECONDS = 300


class RunCommandArgs(BaseModel):
    command: str = Field(..., min_length=1)


def _harden_argv(argv: list[str]) -> list[str]:
    check_argv_allowlisted(argv)
    argv[0] = resolve_executable(argv[0])
    if Path(argv[0]).name == "npm" and "--ignore-scripts" not in argv:
        argv = [*argv, "--ignore-scripts"]
    return argv


def _prepare(command: str) -> list[str]:
    policy_error = check_command_allowed(command)
    if policy_error is not None:
        raise CommandPolicyError(policy_error)
    return _harden_argv(shlex.split(command))


def _timeout_result() -> ToolResult:
    return ToolResult(
        ok=False,
        output=f"Command timed out after {_DEFAULT_TIMEOUT_SECONDS}s.",
        error="timeout",
    )


def _result(stdout: str, stderr: str, returncode: int, argv: list[str]) -> ToolResult:
    combined = stdout + stderr
    ok = returncode == 0
    return ToolResult(
        ok=ok,
        output=combined or f"(exit {returncode})",
        data={"returncode": returncode, "argv": argv},
        error=None if ok else f"exit {returncode}",
    )


class RunCommandTool:
    name = "run_command"
    description = "Run an allowlisted shell command in the workspace root."
    args_schema = RunCommandArgs

    async def aexecute(self, args: BaseModel, *, workspace_root: Path) -> ToolResult:
        """The path agents actually take (ToolRegistry.adispatch).

        Native async, not ``to_thread(execute)``: a thread is not cancellable and this
        child can live 300 s past a Stop. See AGENTS.md §3.1.
        """
        assert isinstance(args, RunCommandArgs)
        try:
            argv = _prepare(args.command)
        except (ToolError, CommandPolicyError) as exc:
            return ToolResult(ok=False, output=str(exc), error="not allowed")

        result = await run_argv(
            argv,
            cwd=workspace_root.resolve(strict=False),
            env=sandbox_env(),
            timeout=_DEFAULT_TIMEOUT_SECONDS,
        )
        if result.timed_out:
            return _timeout_result()
        return _result(result.stdout, result.stderr, result.returncode, argv)

    def execute(self, args: BaseModel, *, workspace_root: Path) -> ToolResult:
        """Blocking fallback for the sync registry. Shares _prepare/_result with
        ``aexecute``; the only difference is that a cancelled caller cannot stop it."""
        assert isinstance(args, RunCommandArgs)
        try:
            argv = _prepare(args.command)
        except (ToolError, CommandPolicyError) as exc:
            return ToolResult(ok=False, output=str(exc), error="not allowed")

        try:
            proc = subprocess.run(
                argv,
                cwd=str(workspace_root.resolve(strict=False)),
                env=sandbox_env(),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=_DEFAULT_TIMEOUT_SECONDS,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return _timeout_result()

        return _result(proc.stdout or "", proc.stderr or "", proc.returncode, argv)


run_command_tool = RunCommandTool()
