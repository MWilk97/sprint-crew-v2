from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, Field

from sprint_crew.tools._safety import resolve_safe_path
from sprint_crew.tools.base import ToolResult


class ListDirectoryArgs(BaseModel):
    path: str = Field(default=".")


class ListDirectoryTool:
    name = "list_directory"
    description = "List files and directories under a workspace path."
    args_schema = ListDirectoryArgs

    def execute(self, args: BaseModel, *, workspace_root: Path) -> ToolResult:
        assert isinstance(args, ListDirectoryArgs)
        target = resolve_safe_path(args.path, root=workspace_root)
        if not target.exists():
            return ToolResult(ok=False, output=f"Path not found: {args.path!r}", error="not found")
        if not target.is_dir():
            return ToolResult(ok=False, output=f"Not a directory: {args.path!r}", error="not a directory")
        entries = sorted(target.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
        lines = []
        for entry in entries:
            rel = entry.relative_to(workspace_root.resolve(strict=False))
            kind = "dir" if entry.is_dir() else "file"
            lines.append(f"{kind}\t{rel}")
        body = "\n".join(lines) if lines else "(empty directory)"
        return ToolResult(ok=True, output=body, data={"count": len(lines)})


list_directory_tool = ListDirectoryTool()
