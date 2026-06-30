from __future__ import annotations

import re
from pathlib import Path

from pydantic import BaseModel, Field

from sprint_crew.tools._safety import resolve_safe_path
from sprint_crew.tools.base import ToolResult


class GrepArgs(BaseModel):
    pattern: str = Field(..., min_length=1)
    path: str = Field(default=".")


class GrepTool:
    name = "grep"
    description = "Search file contents under a workspace path for a regex pattern."
    args_schema = GrepArgs

    def execute(self, args: BaseModel, *, workspace_root: Path) -> ToolResult:
        assert isinstance(args, GrepArgs)
        root = resolve_safe_path(args.path, root=workspace_root)
        if not root.exists():
            return ToolResult(ok=False, output=f"Path not found: {args.path!r}", error="not found")
        try:
            regex = re.compile(args.pattern)
        except re.error as exc:
            return ToolResult(ok=False, output=f"Invalid regex: {exc}", error="bad pattern")

        hits: list[str] = []
        files = [root] if root.is_file() else sorted(root.rglob("*"))
        for file_path in files:
            if not file_path.is_file():
                continue
            try:
                file_path.relative_to(workspace_root.resolve(strict=False))
            except ValueError:
                continue
            try:
                for i, line in enumerate(
                    file_path.read_text(encoding="utf-8", errors="replace").splitlines(), start=1
                ):
                    if regex.search(line):
                        rel = file_path.relative_to(workspace_root.resolve(strict=False))
                        hits.append(f"{rel}:{i}:{line}")
            except OSError:
                continue
            if len(hits) >= 200:
                break

        body = "\n".join(hits) if hits else "(no matches)"
        return ToolResult(ok=True, output=body, data={"matches": len(hits)})


grep_tool = GrepTool()
