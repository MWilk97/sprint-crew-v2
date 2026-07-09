from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, Field

from sprint_crew.tools._safety import resolve_safe_path
from sprint_crew.tools.base import ToolResult


class ReadFileArgs(BaseModel):
    path: str = Field(..., min_length=1)
    start_line: int | None = Field(default=None, ge=1)
    end_line: int | None = Field(default=None, ge=1)


class ReadFileTool:
    name = "read_file"
    description = "Read a UTF-8 text file from the workspace (optional line range)."
    args_schema = ReadFileArgs

    def execute(self, args: BaseModel, *, workspace_root: Path) -> ToolResult:
        assert isinstance(args, ReadFileArgs)
        target = resolve_safe_path(args.path, root=workspace_root)
        if not target.is_file():
            return ToolResult(ok=False, output=f"Not a file: {args.path!r}", error="not a file")
        text = target.read_text(encoding="utf-8", errors="replace")
        lines = text.splitlines(keepends=True)
        if args.start_line is not None or args.end_line is not None:
            start = (args.start_line or 1) - 1
            end = args.end_line if args.end_line is not None else len(lines)
            snippet = "".join(lines[start:end])
            return ToolResult(
                ok=True, output=snippet, data={"path": args.path, "lines": len(lines)}
            )
        return ToolResult(ok=True, output=text, data={"path": args.path, "lines": len(lines)})


read_file_tool = ReadFileTool()
