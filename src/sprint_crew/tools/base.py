from __future__ import annotations

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

    def execute(self, args: BaseModel, *, workspace_root: Path) -> ToolResult:
        ...


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
            raise TypeError(f"Tool {tool.name!r}.args_schema must be a Pydantic BaseModel subclass.")
        if tool.name in self._tools:
            raise ValueError(f"Tool {tool.name!r} is already registered.")
        self._tools[tool.name] = tool

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def names(self) -> list[str]:
        return sorted(self._tools.keys())

    def list_tools(self) -> list[Tool]:
        return [self._tools[name] for name in self.names()]

    def dispatch(
        self,
        name: str,
        raw_args: dict[str, Any] | None,
        *,
        workspace_root: Path,
    ) -> ToolResult:
        tool = self._tools.get(name)
        if tool is None:
            available = ", ".join(self.names()) or "(none)"
            return ToolResult(
                ok=False,
                output=f"Unknown tool {name!r}. Available: {available}.",
                error="unknown tool",
            )
        try:
            args = tool.args_schema.model_validate(raw_args or {})
        except ValidationError as exc:
            errors = exc.errors(include_url=False)
            return ToolResult(
                ok=False,
                output=f"Invalid arguments for {name!r}: {errors}",
                error="invalid arguments",
            )
        try:
            return tool.execute(args, workspace_root=workspace_root)
        except ToolError:
            raise
        except UnsafePathError as exc:
            return ToolResult(
                ok=False,
                output=f"Path safety check failed: {exc}",
                error="unsafe path",
            )
        except (FileNotFoundError, PermissionError, IsADirectoryError, NotADirectoryError) as exc:
            return ToolResult(
                ok=False,
                output=f"{name!r} filesystem error: {exc}",
                error=type(exc).__name__,
            )
        except OSError as exc:
            return ToolResult(
                ok=False,
                output=f"{name!r} OS error: {exc}",
                error=type(exc).__name__,
            )
        except Exception as exc:
            log.exception("Tool %r crashed unexpectedly", name)
            return ToolResult(
                ok=False,
                output=f"Tool {name!r} crashed: {exc}",
                error="unexpected",
            )
