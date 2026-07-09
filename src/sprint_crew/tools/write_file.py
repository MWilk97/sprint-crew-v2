from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, Field

from sprint_crew.config import get_settings
from sprint_crew.tools._safety import resolve_safe_path
from sprint_crew.tools.base import ToolResult


class WriteFileArgs(BaseModel):
    path: str = Field(..., min_length=1)
    content: str = Field(default="")


class WriteFileTool:
    name = "write_file"
    description = "Write UTF-8 content to a file in the workspace (creates parent dirs)."
    args_schema = WriteFileArgs

    def execute(self, args: BaseModel, *, workspace_root: Path) -> ToolResult:
        assert isinstance(args, WriteFileArgs)
        content_bytes = len(args.content.encode("utf-8"))
        max_bytes = get_settings().max_write_file_bytes
        if content_bytes > max_bytes:
            return ToolResult(
                ok=False,
                output=(
                    f"Refusing write_file: {content_bytes} bytes exceeds limit of {max_bytes}. "
                    "Use apply_patch for large edits."
                ),
                error="write too large",
            )
        target = resolve_safe_path(args.path, root=workspace_root)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(args.content, encoding="utf-8")
        return ToolResult(
            ok=True,
            output=f"Wrote {len(args.content)} bytes to {args.path!r}.",
            data={"path": args.path, "bytes": len(args.content.encode())},
        )


write_file_tool = WriteFileTool()
