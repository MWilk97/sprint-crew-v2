from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, Field

from sprint_crew.tools._git_utils import find_git_dir, run_git
from sprint_crew.tools.base import ToolResult


class GitStatusArgs(BaseModel):
    pass


class GitDiffArgs(BaseModel):
    pass


class GitLogArgs(BaseModel):
    n: int = Field(default=10, ge=1, le=100)


def _git_root(workspace_root: Path) -> tuple[Path, Path] | None:
    git_dir = find_git_dir(workspace_root)
    if git_dir is None:
        return None
    # The work tree is the workspace either way; find_git_dir already resolved whether
    # .git is a directory or a worktree pointer file, which is the only thing that varies.
    return workspace_root.resolve(strict=False), git_dir.resolve(strict=False)


class GitStatusTool:
    name = "git_status"
    description = "Show git status for the workspace repository."
    args_schema = GitStatusArgs

    def execute(self, args: BaseModel, *, workspace_root: Path) -> ToolResult:
        ctx = _git_root(workspace_root)
        if ctx is None:
            return ToolResult(ok=False, output="Not a git repository.", error="no git")
        work_tree, git_dir = ctx
        code, out, err = run_git(
            ["status", "--short", "--branch"], work_tree=work_tree, git_dir=git_dir
        )
        body = out + err
        return ToolResult(ok=code == 0, output=body or "(clean)", data={"returncode": code})


class GitDiffTool:
    name = "git_diff"
    description = "Show git diff for the workspace repository."
    args_schema = GitDiffArgs

    def execute(self, args: BaseModel, *, workspace_root: Path) -> ToolResult:
        ctx = _git_root(workspace_root)
        if ctx is None:
            return ToolResult(ok=False, output="Not a git repository.", error="no git")
        work_tree, git_dir = ctx
        code, out, err = run_git(["diff"], work_tree=work_tree, git_dir=git_dir)
        body = out + err
        return ToolResult(ok=True, output=body or "(no diff)", data={"returncode": code})


class GitLogTool:
    name = "git_log"
    description = "Show recent git log entries."
    args_schema = GitLogArgs

    def execute(self, args: BaseModel, *, workspace_root: Path) -> ToolResult:
        assert isinstance(args, GitLogArgs)
        ctx = _git_root(workspace_root)
        if ctx is None:
            return ToolResult(ok=False, output="Not a git repository.", error="no git")
        work_tree, git_dir = ctx
        code, out, err = run_git(
            ["log", f"-{args.n}", "--oneline", "--decorate"],
            work_tree=work_tree,
            git_dir=git_dir,
        )
        body = out + err
        return ToolResult(ok=code == 0, output=body or "(empty log)", data={"returncode": code})


git_status_tool = GitStatusTool()
git_diff_tool = GitDiffTool()
git_log_tool = GitLogTool()
