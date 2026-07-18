from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pydantic import BaseModel
from pydantic_ai import RunContext
from pydantic_ai.toolsets.function import FunctionToolset

from sprint_crew.agents.tool_events import ToolCallLog
from sprint_crew.config import get_settings
from sprint_crew.orchestrator.plan_coverage import (
    check_mutation_allowed,
    check_patch_mutations_allowed,
    validate_plan_coverage,
)
from sprint_crew.orchestrator.pytest_cmd import normalize_test_command
from sprint_crew.orchestrator.workspace_diff import gather_workspace_diff
from sprint_crew.schemas.ticket import TaskPlan
from sprint_crew.tools import ALL_TOOLS, READONLY_TOOLS, ToolRegistry, build_registry
from sprint_crew.tools.apply_patch import ApplyPatchArgs
from sprint_crew.tools.git_tools import GitLogArgs
from sprint_crew.tools.grep import GrepArgs
from sprint_crew.tools.list_directory import ListDirectoryArgs
from sprint_crew.tools.read_file import ReadFileArgs
from sprint_crew.tools.run_command import RunCommandArgs
from sprint_crew.tools.semantic_search import SemanticSearchArgs, semantic_search_tool
from sprint_crew.tools.write_file import WriteFileArgs


@dataclass
class WorkspaceDeps:
    root: Path
    registry: ToolRegistry
    session_id: str | None = None
    acceptance_tests: tuple[str, ...] = field(default_factory=tuple)
    early_exit_handoff: str | None = None
    tool_call_log: ToolCallLog | None = None
    task_plan: TaskPlan | None = None
    require_full_coverage: bool = False
    baseline_paths: frozenset[str] | None = None
    _workspace_dirty: bool = field(default=False, repr=False)
    _cached_diff: str | None = field(default=None, repr=False)
    _cached_coverage: Any = field(default=None, repr=False)


def _invalidate_workspace_cache(deps: WorkspaceDeps) -> None:
    deps._workspace_dirty = True
    deps._cached_diff = None
    deps._cached_coverage = None


def _cached_workspace_diff(deps: WorkspaceDeps, *, max_chars: int = 2000) -> str:
    if not deps._workspace_dirty and deps._cached_diff is not None:
        return deps._cached_diff
    diff = gather_workspace_diff(deps.root, max_chars=max_chars)
    deps._cached_diff = diff
    deps._workspace_dirty = False
    return diff


def _cached_plan_coverage(deps: WorkspaceDeps):
    if deps.task_plan is None:
        return None
    if not deps._workspace_dirty and deps._cached_coverage is not None:
        return deps._cached_coverage
    coverage = validate_plan_coverage(
        deps.task_plan,
        deps.root,
        baseline_paths=deps.baseline_paths,
    )
    deps._cached_coverage = coverage
    deps._workspace_dirty = False
    return coverage


def _record_tool_call(
    deps: WorkspaceDeps,
    name: str,
    args: dict[str, Any],
    output: str,
    *,
    ok: bool,
) -> None:
    if deps.tool_call_log is None:
        return
    preview = output if len(output) <= 500 else output[:500] + "…"
    deps.tool_call_log.append(
        {
            "tool": name,
            "args": args,
            "ok": ok,
            "output_preview": preview,
        }
    )


_SOFT_FAIL_READONLY_TOOLS = frozenset(
    {"grep", "read_file", "list_directory", "git_status", "git_diff", "git_log", "semantic_search"}
)


def _dispatch(ctx: RunContext[WorkspaceDeps], name: str, args: BaseModel) -> str:
    payload = args.model_dump(exclude_none=True)
    result = ctx.deps.registry.dispatch(
        name,
        payload,
        workspace_root=ctx.deps.root,
    )
    output = result.output
    if not result.ok and name in _SOFT_FAIL_READONLY_TOOLS:
        output = f"[tool error] {output}"
    _record_tool_call(ctx.deps, name, payload, output, ok=result.ok)
    return output


def _dispatch_result(ctx: RunContext[WorkspaceDeps], name: str, args: BaseModel):
    payload = args.model_dump(exclude_none=True)
    result = ctx.deps.registry.dispatch(
        name,
        payload,
        workspace_root=ctx.deps.root,
    )
    _record_tool_call(ctx.deps, name, payload, result.output, ok=result.ok)
    return result


def _coder_scope_error_for_path(deps: WorkspaceDeps, path: str) -> str | None:
    if deps.task_plan is None:
        return None
    return check_mutation_allowed(path, deps.task_plan)


def _coder_scope_error_for_patch(deps: WorkspaceDeps, patch: str) -> str | None:
    if deps.task_plan is None:
        return None
    return check_patch_mutations_allowed(patch, deps.task_plan)


def _maybe_trigger_early_exit(ctx: RunContext[WorkspaceDeps], command: str, *, ok: bool) -> None:
    if not ok or not ctx.deps.acceptance_tests or ctx.deps.early_exit_handoff:
        return
    normalized = normalize_test_command(command.strip(), ctx.deps.root)
    matches = any(
        normalized == normalize_test_command(ac.strip(), ctx.deps.root)
        for ac in ctx.deps.acceptance_tests
    )
    if not matches:
        return
    diff = _cached_workspace_diff(ctx.deps, max_chars=2000)
    if not diff.strip():
        return
    if ctx.deps.require_full_coverage and ctx.deps.task_plan is not None:
        coverage = _cached_plan_coverage(ctx.deps)
        if coverage is not None and not coverage.satisfied:
            return
    ctx.deps.early_exit_handoff = (
        "Acceptance tests passed and workspace has changes. Handoff for Formatter.\n"
        f"Last test command: {command.strip()}\n"
        f"Diff preview:\n{diff[:1500]}"
    )


def build_coder_toolset() -> FunctionToolset[WorkspaceDeps]:
    ts: FunctionToolset[WorkspaceDeps] = FunctionToolset()

    async def read_file(
        ctx: RunContext[WorkspaceDeps],
        path: str,
        start_line: int | None = None,
        end_line: int | None = None,
    ) -> str:
        """Read a UTF-8 text file from the workspace (optional line range)."""
        return _dispatch(
            ctx, "read_file", ReadFileArgs(path=path, start_line=start_line, end_line=end_line)
        )

    async def write_file(ctx: RunContext[WorkspaceDeps], path: str, content: str = "") -> str:
        """Write UTF-8 content to a file in the workspace."""
        err = _coder_scope_error_for_path(ctx.deps, path)
        if err:
            _record_tool_call(ctx.deps, "write_file", {"path": path}, err, ok=False)
            return err
        _invalidate_workspace_cache(ctx.deps)
        return _dispatch(ctx, "write_file", WriteFileArgs(path=path, content=content))

    async def apply_patch(ctx: RunContext[WorkspaceDeps], patch: str) -> str:
        """Apply a unified diff patch within the workspace."""
        err = _coder_scope_error_for_patch(ctx.deps, patch)
        if err:
            _record_tool_call(ctx.deps, "apply_patch", {"patch": patch[:200]}, err, ok=False)
            return err
        _invalidate_workspace_cache(ctx.deps)
        return _dispatch(ctx, "apply_patch", ApplyPatchArgs(patch=patch))

    async def grep(ctx: RunContext[WorkspaceDeps], pattern: str, path: str = ".") -> str:
        """Search file contents under a workspace path for a regex pattern."""
        return _dispatch(ctx, "grep", GrepArgs(pattern=pattern, path=path))

    async def list_directory(ctx: RunContext[WorkspaceDeps], path: str = ".") -> str:
        """List files and directories under a workspace path."""
        return _dispatch(ctx, "list_directory", ListDirectoryArgs(path=path))

    async def run_command(ctx: RunContext[WorkspaceDeps], command: str) -> str:
        """Run an allowlisted shell command in the workspace root."""
        result = _dispatch_result(ctx, "run_command", RunCommandArgs(command=command))
        _maybe_trigger_early_exit(ctx, command, ok=result.ok)
        return result.output

    async def git_status(ctx: RunContext[WorkspaceDeps]) -> str:
        """Show git status for the workspace repository."""
        from sprint_crew.tools.git_tools import GitStatusArgs

        return _dispatch(ctx, "git_status", GitStatusArgs())

    for fn in (
        read_file,
        write_file,
        apply_patch,
        grep,
        list_directory,
        run_command,
        git_status,
    ):
        ts.tool(fn)
    return ts


def build_tester_toolset() -> FunctionToolset[WorkspaceDeps]:
    ts: FunctionToolset[WorkspaceDeps] = FunctionToolset()

    async def read_file(
        ctx: RunContext[WorkspaceDeps],
        path: str,
        start_line: int | None = None,
        end_line: int | None = None,
    ) -> str:
        """Read a UTF-8 text file from the workspace."""
        return _dispatch(
            ctx, "read_file", ReadFileArgs(path=path, start_line=start_line, end_line=end_line)
        )

    async def write_file(ctx: RunContext[WorkspaceDeps], path: str, content: str = "") -> str:
        """Write UTF-8 content to a file under tests/."""
        err = check_mutation_allowed(path, None, tests_only=True)
        if err:
            _record_tool_call(ctx.deps, "write_file", {"path": path}, err, ok=False)
            return err
        return _dispatch(ctx, "write_file", WriteFileArgs(path=path, content=content))

    async def apply_patch(ctx: RunContext[WorkspaceDeps], patch: str) -> str:
        """Apply a unified diff patch to files under tests/."""
        err = check_patch_mutations_allowed(patch, None, tests_only=True)
        if err:
            _record_tool_call(ctx.deps, "apply_patch", {"patch": patch[:200]}, err, ok=False)
            return err
        return _dispatch(ctx, "apply_patch", ApplyPatchArgs(patch=patch))

    async def grep(ctx: RunContext[WorkspaceDeps], pattern: str, path: str = ".") -> str:
        """Search file contents for a regex pattern."""
        return _dispatch(ctx, "grep", GrepArgs(pattern=pattern, path=path))

    async def list_directory(ctx: RunContext[WorkspaceDeps], path: str = ".") -> str:
        """List directory entries."""
        return _dispatch(ctx, "list_directory", ListDirectoryArgs(path=path))

    async def run_command(ctx: RunContext[WorkspaceDeps], command: str) -> str:
        """Run an allowlisted shell command."""
        return _dispatch(ctx, "run_command", RunCommandArgs(command=command))

    async def git_status(ctx: RunContext[WorkspaceDeps]) -> str:
        """Show git status."""
        from sprint_crew.tools.git_tools import GitStatusArgs

        return _dispatch(ctx, "git_status", GitStatusArgs())

    for fn in (read_file, write_file, apply_patch, grep, list_directory, run_command, git_status):
        ts.tool(fn)
    return ts


def build_readonly_toolset(
    *, include_semantic_search: bool = False
) -> FunctionToolset[WorkspaceDeps]:
    ts: FunctionToolset[WorkspaceDeps] = FunctionToolset()

    async def read_file(
        ctx: RunContext[WorkspaceDeps],
        path: str,
        start_line: int | None = None,
        end_line: int | None = None,
    ) -> str:
        """Read a UTF-8 text file from the workspace."""
        return _dispatch(
            ctx, "read_file", ReadFileArgs(path=path, start_line=start_line, end_line=end_line)
        )

    async def grep(ctx: RunContext[WorkspaceDeps], pattern: str, path: str = ".") -> str:
        """Search file contents for a regex pattern."""
        return _dispatch(ctx, "grep", GrepArgs(pattern=pattern, path=path))

    async def list_directory(ctx: RunContext[WorkspaceDeps], path: str = ".") -> str:
        """List directory entries."""
        return _dispatch(ctx, "list_directory", ListDirectoryArgs(path=path))

    async def run_command(ctx: RunContext[WorkspaceDeps], command: str) -> str:
        """Run an allowlisted shell command."""
        return _dispatch(ctx, "run_command", RunCommandArgs(command=command))

    async def git_status(ctx: RunContext[WorkspaceDeps]) -> str:
        """Show git status."""
        from sprint_crew.tools.git_tools import GitStatusArgs

        return _dispatch(ctx, "git_status", GitStatusArgs())

    async def git_diff(ctx: RunContext[WorkspaceDeps]) -> str:
        """Show git diff."""
        from sprint_crew.tools.git_tools import GitDiffArgs

        return _dispatch(ctx, "git_diff", GitDiffArgs())

    async def git_log(ctx: RunContext[WorkspaceDeps], n: int = 10) -> str:
        """Show recent git log."""
        return _dispatch(ctx, "git_log", GitLogArgs(n=n))

    tools = [read_file, grep, list_directory, run_command, git_status, git_diff, git_log]

    if include_semantic_search:

        async def semantic_search(
            ctx: RunContext[WorkspaceDeps],
            query: str,
            path_prefix: str | None = None,
            top_k: int | None = None,
            chunk_kind: str | None = None,
        ) -> str:
            """Semantic search over indexed workspace code (concept-level discovery)."""
            return _dispatch(
                ctx,
                "semantic_search",
                SemanticSearchArgs(
                    query=query,
                    path_prefix=path_prefix,
                    top_k=top_k,
                    chunk_kind=chunk_kind,
                ),
            )

        tools.append(semantic_search)

    for fn in tools:
        ts.tool(fn)
    return ts


def workspace_deps(
    root: Path,
    *,
    mutate: bool = True,
    session_id: str | None = None,
    include_semantic_search: bool = False,
    acceptance_tests: tuple[str, ...] | None = None,
    tool_call_log: ToolCallLog | None = None,
    task_plan: TaskPlan | None = None,
    require_full_coverage: bool | None = None,
    baseline_paths: frozenset[str] | None = None,
) -> WorkspaceDeps:
    tools = list(ALL_TOOLS if mutate else READONLY_TOOLS)
    settings = get_settings()
    if include_semantic_search and settings.vector_index_enabled:
        tools.append(semantic_search_tool(session_id or root.name))
    if require_full_coverage is None:
        require_full_coverage = settings.coder_early_exit_requires_coverage
    return WorkspaceDeps(
        root=root.resolve(),
        registry=build_registry(tools),
        session_id=session_id or root.name,
        acceptance_tests=acceptance_tests or (),
        tool_call_log=tool_call_log,
        task_plan=task_plan,
        require_full_coverage=require_full_coverage,
        baseline_paths=baseline_paths,
    )
