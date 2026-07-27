from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel, ValidationError

from sprint_crew.tools._safety import UnsafePathError

log = logging.getLogger(__name__)


class ToolError(RuntimeError):
    pass


class ToolResult(BaseModel):
    ok: bool
    output: str
    data: dict[str, Any] | None = None
    error: str | None = None


@runtime_checkable
class Tool(Protocol):
    name: str
    description: str
    args_schema: type[BaseModel]

    def execute(self, args: BaseModel, *, workspace_root: Path) -> ToolResult: ...


@runtime_checkable
class AsyncTool(Protocol):
    """A tool that can also run without occupying a thread.

    Required for any tool whose child process can outlive a cancelled run — a thread
    running ``subprocess.run`` cannot be interrupted, so the child survives Stop. Today
    that is ``run_command`` (300 s cap) alone: grep and the git tools are bounded by
    ``GIT_TIMEOUT_S`` and finish in well under a second, so paying for ``aexecute`` there
    would buy nothing. Add one here before adding another long-running tool.
    """

    async def aexecute(self, args: BaseModel, *, workspace_root: Path) -> ToolResult: ...


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        missing: list[str] = []
        for attr in ("name", "description", "args_schema"):
            if not hasattr(tool, attr):
                missing.append(attr)
        if not callable(getattr(tool, "execute", None)):
            missing.append("execute")
        if missing:
            raise TypeError(f"{tool!r} is not a Tool: missing {sorted(missing)}.")
        if not isinstance(tool.name, str) or not tool.name.strip():
            raise ValueError("Tool.name must be a non-empty string.")
        if not isinstance(tool.description, str) or not tool.description.strip():
            raise ValueError(f"Tool {tool.name!r}.description must be non-empty.")
        if not (isinstance(tool.args_schema, type) and issubclass(tool.args_schema, BaseModel)):
            raise TypeError(
                f"Tool {tool.name!r}.args_schema must be a Pydantic BaseModel subclass."
            )
        if tool.name in self._tools:
            raise ValueError(f"Tool {tool.name!r} is already registered.")
        self._tools[tool.name] = tool

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def names(self) -> list[str]:
        return sorted(self._tools.keys())

    def _resolve(
        self, name: str, raw_args: dict[str, Any] | None
    ) -> tuple[Tool, BaseModel] | ToolResult:
        """Look up the tool and validate its arguments, or return the failure to report."""
        tool = self._tools.get(name)
        if tool is None:
            available = ", ".join(self.names()) or "(none)"
            return ToolResult(
                ok=False,
                output=f"Unknown tool {name!r}. Available: {available}.",
                error="unknown tool",
            )
        try:
            return tool, tool.args_schema.model_validate(raw_args or {})
        except ValidationError as exc:
            errors = exc.errors(include_url=False)
            return ToolResult(
                ok=False,
                output=f"Invalid arguments for {name!r}: {errors}",
                error="invalid arguments",
            )

    def dispatch(
        self,
        name: str,
        raw_args: dict[str, Any] | None,
        *,
        workspace_root: Path,
    ) -> ToolResult:
        resolved = self._resolve(name, raw_args)
        if isinstance(resolved, ToolResult):
            return resolved
        tool, args = resolved
        try:
            return tool.execute(args, workspace_root=workspace_root)
        except Exception as exc:
            return _map_tool_exception(name, exc)

    async def adispatch(
        self,
        name: str,
        raw_args: dict[str, Any] | None,
        *,
        workspace_root: Path,
    ) -> ToolResult:
        """Dispatch without holding the event loop.

        A tool that implements ``aexecute`` (see ``AsyncTool``) is awaited directly, so
        cancelling the caller kills its child. Everything else is a blocking body that is
        short enough to hand to a worker thread.
        """
        resolved = self._resolve(name, raw_args)
        if isinstance(resolved, ToolResult):
            return resolved
        tool, args = resolved
        try:
            if isinstance(tool, AsyncTool):
                return await tool.aexecute(args, workspace_root=workspace_root)
            return await asyncio.to_thread(tool.execute, args, workspace_root=workspace_root)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            return _map_tool_exception(name, exc)


def _map_tool_exception(name: str, exc: Exception) -> ToolResult:
    """Turn a tool crash into a reportable result. Shared so the two dispatch paths agree."""
    if isinstance(exc, ToolError):
        raise exc
    if isinstance(exc, UnsafePathError):
        return ToolResult(
            ok=False,
            output=f"Path safety check failed: {exc}",
            error="unsafe path",
        )
    if isinstance(
        exc, FileNotFoundError | PermissionError | IsADirectoryError | NotADirectoryError
    ):
        return ToolResult(
            ok=False,
            output=f"{name!r} filesystem error: {exc}",
            error=type(exc).__name__,
        )
    if isinstance(exc, OSError):
        return ToolResult(
            ok=False,
            output=f"{name!r} OS error: {exc}",
            error=type(exc).__name__,
        )
    log.exception("Tool %r crashed unexpectedly", name, exc_info=exc)
    return ToolResult(
        ok=False,
        output=f"Tool {name!r} crashed: {exc}",
        error="unexpected",
    )
