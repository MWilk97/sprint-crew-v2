from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path

from pydantic import BaseModel, Field

from sprint_crew.tools.base import ToolResult


class ApplyPatchArgs(BaseModel):
    patch: str = Field(..., min_length=1)


class ApplyPatchTool:
    name = "apply_patch"
    description = "Apply a unified diff patch within the workspace using patch(1)."
    args_schema = ApplyPatchArgs

    def execute(self, args: BaseModel, *, workspace_root: Path) -> ToolResult:
        assert isinstance(args, ApplyPatchArgs)
        root = workspace_root.resolve(strict=False)
        with tempfile.NamedTemporaryFile("w", suffix=".patch", delete=False, encoding="utf-8") as fh:
            fh.write(args.patch)
            patch_path = fh.name
        try:
            proc = subprocess.run(
                ["patch", "-p1", "--forward", "--batch", "-i", patch_path],
                cwd=str(root),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=60,
                check=False,
            )
        except FileNotFoundError:
            return ToolResult(
                ok=False,
                output="patch(1) not installed on host.",
                error="patch missing",
            )
        except subprocess.TimeoutExpired:
            return ToolResult(ok=False, output="patch timed out.", error="timeout")
        finally:
            Path(patch_path).unlink(missing_ok=True)

        out = (proc.stdout or "") + (proc.stderr or "")
        if proc.returncode != 0:
            return ToolResult(ok=False, output=out or "patch failed.", error="patch failed")
        return ToolResult(ok=True, output=out or "Patch applied.", data={"returncode": proc.returncode})


apply_patch_tool = ApplyPatchTool()
