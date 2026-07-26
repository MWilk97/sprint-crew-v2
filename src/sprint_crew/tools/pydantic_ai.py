from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pydantic import BaseModel
from pydantic_ai import RunContext
from pydantic_ai.toolsets.function import FunctionToolset

from sprint_crew.agents.tool_events import ToolCallLog, tool_call_event
from sprint_crew.config import get_settings
from sprint_crew.orchestrator.emitter import emit_live
from sprint_crew.orchestrator.plan_coverage import (
    check_mutation_allowed,
    check_patch_mutations_allowed,
    validate_plan_coverage,
)
from sprint_crew.orchestrator.pytest_cmd import normalize_test_command
from sprint_crew.orchestrator.workspace_diff import gather_workspace_diff
from sprint_crew.schemas.session import utc_now_iso
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
    event_agent: str = "agent"
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


_MAX_ARG_CHARS = 1000


def _cap_args(args: dict[str, Any]) -> dict[str, Any]:
    """Truncate oversized string args (a successful apply_patch/write_file carries the
    entire file/patch text). ``output_preview`` is already capped; this caps the input side.
    """
    capped: dict[str, Any] = {}
    for key, value in args.items():
        if isinstance(value, str) and len(value) > _MAX_ARG_CHARS:
            capped[key] = value[:_MAX_ARG_CHARS] + "…"
        else:
            capped[key] = value
    return capped


def _record_tool_call(
    deps: WorkspaceDeps,
    name: str,
    args: dict[str, Any],
    output: str,
    *,
    ok: bool,
    duration_ms: float | None = None,
) -> None:
    if deps.tool_call_log is None:
        return
    preview = output if len(output) <= 500 else output[:500] + "…"
    entry: dict[str, Any] = {
        "tool": name,
        "args": _cap_args(args),
        "ok": ok,
        "output_preview": preview,
        "timestamp": utc_now_iso(),
    }
    if duration_ms is not None:
        entry["duration_ms"] = round(duration_ms, 1)
    deps.tool_call_log.append(entry)
    # M4: publish live so the tool event streams the instant it returns, and keep the seq
    # it was assigned on the entry — the node-end batch conversion rebuilds this same event
    # and uses that seq to know it is already in the timeline. When no emitter is set (plain
    # sprint runs, most unit tests) seq stays absent and the batch path is the only writer.
    # See orchestrator/emitter.py for the reconciliation contract.
    published = emit_live(tool_call_event(deps.event_agent, entry, len(deps.tool_call_log)))
    if published is not None:
        entry["seq"] = published.seq


_SOFT_FAIL_READONLY_TOOLS = frozenset(
    {"grep", "read_file", "list_directory", "git_status", "git_diff", "git_log", "semantic_search"}
)


async def _dispatch_off_loop(ctx: RunContext[WorkspaceDeps], name: str, payload: dict[str, Any]):
    """Run the tool body in a worker thread.

    Every tool here is blocking — run_command shells out with a 300 s cap, the git tools
    and grep spawn subprocesses, read/write hit the filesystem. The closures are async, so
    calling the registry directly held the event loop for the whole tool call on *every*
    coder and tester turn: no SSE frame flushed, /health could not answer, and POST /cancel
    could not even be accepted.

    Only the dispatch moves off the loop. _record_tool_call stays on it because it publishes
    to the event log, and the seq it assigns has to stay ordered against the node-end batch
    (orchestrator/emitter.py).
    """
    return await asyncio.to_thread(
        ctx.deps.registry.dispatch,
        name,
        payload,
        workspace_root=ctx.deps.root,
    )


async def _dispatch(ctx: RunContext[WorkspaceDeps], name: str, args: BaseModel) -> str:
    payload = args.model_dump(exclude_none=True)
    t0 = time.monotonic()
    result = await _dispatch_off_loop(ctx, name, payload)
    duration_ms = (time.monotonic() - t0) * 1000
    output = result.output
    if not result.ok and name in _SOFT_FAIL_READONLY_TOOLS:
        output = f"[tool error] {output}"
    _record_tool_call(ctx.deps, name, payload, output, ok=result.ok, duration_ms=duration_ms)
    return output


async def _dispatch_result(ctx: RunContext[WorkspaceDeps], name: str, args: BaseModel):
    payload = args.model_dump(exclude_none=True)
    t0 = time.monotonic()
    result = await _dispatch_off_loop(ctx, name, payload)
    duration_ms = (time.monotonic() - t0) * 1000
    _record_tool_call(ctx.deps, name, payload, result.output, ok=result.ok, duration_ms=duration_ms)
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


_TOOL_DESCRIPTIONS: dict[str, dict[str, str]] = {
    "coder": {
        "read_file": "Read a UTF-8 text file from the workspace (optional line range).",
        "write_file": "Write UTF-8 content to a file in the workspace.",
        "apply_patch": "Apply a unified diff patch within the workspace.",
        "grep": "Search file contents under a workspace path for a regex pattern.",
        "list_directory": "List files and directories under a workspace path.",
        "run_command": "Run an allowlisted shell command in the workspace root.",
        "git_status": "Show git status for the workspace repository.",
    },
    "tester": {
        "read_file": "Read a UTF-8 text file from the workspace.",
        "write_file": "Write UTF-8 content to a file under tests/.",
        "apply_patch": "Apply a unified diff patch to files under tests/.",
        "grep": "Search file contents for a regex pattern.",
        "list_directory": "List directory entries.",
        "run_command": "Run an allowlisted shell command.",
        "git_status": "Show git status.",
    },
    "readonly": {
        "read_file": "Read a UTF-8 text file from the workspace.",
        "grep": "Search file contents for a regex pattern.",
        "list_directory": "List directory entries.",
        "run_command": "Run an allowlisted shell command.",
        "git_status": "Show git status.",
        "git_diff": "Show git diff.",
        "git_log": "Show recent git log.",
    },
}


def _build_toolset(
    variant: str,
    *,
    include_semantic_search: bool = False,
) -> FunctionToolset[WorkspaceDeps]:
    """Single toolset factory; ``variant`` selects mutation guards and tool surface.

    - ``coder``: plan-scoped writes, early exit on green acceptance command.
    - ``tester``: writes restricted to tests/.
    - ``readonly``: no writes, plus git_diff / git_log (and optional semantic_search).
    """
    descriptions = _TOOL_DESCRIPTIONS[variant]
    mutating = variant in ("coder", "tester")
    ts: FunctionToolset[WorkspaceDeps] = FunctionToolset()

    async def read_file(
        ctx: RunContext[WorkspaceDeps],
        path: str,
        start_line: int | None = None,
        end_line: int | None = None,
    ) -> str:
        return await _dispatch(
            ctx, "read_file", ReadFileArgs(path=path, start_line=start_line, end_line=end_line)
        )

    async def write_file(ctx: RunContext[WorkspaceDeps], path: str, content: str = "") -> str:
        if variant == "tester":
            err = check_mutation_allowed(path, None, tests_only=True)
        else:
            err = _coder_scope_error_for_path(ctx.deps, path)
        if err:
            _record_tool_call(ctx.deps, "write_file", {"path": path}, err, ok=False)
            return err
        if variant == "coder":
            _invalidate_workspace_cache(ctx.deps)
        return await _dispatch(ctx, "write_file", WriteFileArgs(path=path, content=content))

    async def apply_patch(ctx: RunContext[WorkspaceDeps], patch: str) -> str:
        if variant == "tester":
            err = check_patch_mutations_allowed(patch, None, tests_only=True)
        else:
            err = _coder_scope_error_for_patch(ctx.deps, patch)
        if err:
            _record_tool_call(ctx.deps, "apply_patch", {"patch": patch[:200]}, err, ok=False)
            return err
        if variant == "coder":
            _invalidate_workspace_cache(ctx.deps)
        return await _dispatch(ctx, "apply_patch", ApplyPatchArgs(patch=patch))

    async def grep(ctx: RunContext[WorkspaceDeps], pattern: str, path: str = ".") -> str:
        return await _dispatch(ctx, "grep", GrepArgs(pattern=pattern, path=path))

    async def list_directory(ctx: RunContext[WorkspaceDeps], path: str = ".") -> str:
        return await _dispatch(ctx, "list_directory", ListDirectoryArgs(path=path))

    async def run_command(ctx: RunContext[WorkspaceDeps], command: str) -> str:
        result = await _dispatch_result(ctx, "run_command", RunCommandArgs(command=command))
        if variant == "coder":
            await asyncio.to_thread(_maybe_trigger_early_exit, ctx, command, ok=result.ok)
        return result.output

    async def git_status(ctx: RunContext[WorkspaceDeps]) -> str:
        from sprint_crew.tools.git_tools import GitStatusArgs

        return await _dispatch(ctx, "git_status", GitStatusArgs())

    async def git_diff(ctx: RunContext[WorkspaceDeps]) -> str:
        from sprint_crew.tools.git_tools import GitDiffArgs

        return await _dispatch(ctx, "git_diff", GitDiffArgs())

    async def git_log(ctx: RunContext[WorkspaceDeps], n: int = 10) -> str:
        return await _dispatch(ctx, "git_log", GitLogArgs(n=n))

    async def semantic_search(
        ctx: RunContext[WorkspaceDeps],
        query: str,
        path_prefix: str | None = None,
        top_k: int | None = None,
        chunk_kind: str | None = None,
    ) -> str:
        """Semantic search over indexed workspace code (concept-level discovery)."""
        return await _dispatch(
            ctx,
            "semantic_search",
            SemanticSearchArgs(
                query=query,
                path_prefix=path_prefix,
                top_k=top_k,
                chunk_kind=chunk_kind,
            ),
        )

    tools = [read_file]
    if mutating:
        tools.extend([write_file, apply_patch])
    tools.extend([grep, list_directory, run_command, git_status])
    if variant == "readonly":
        tools.extend([git_diff, git_log])
        if include_semantic_search:
            tools.append(semantic_search)

    for fn in tools:
        if fn.__name__ in descriptions:
            fn.__doc__ = descriptions[fn.__name__]
        ts.tool(fn)
    return ts


def build_coder_toolset() -> FunctionToolset[WorkspaceDeps]:
    return _build_toolset("coder")


def build_tester_toolset() -> FunctionToolset[WorkspaceDeps]:
    return _build_toolset("tester")


def build_readonly_toolset(
    *, include_semantic_search: bool = False
) -> FunctionToolset[WorkspaceDeps]:
    return _build_toolset("readonly", include_semantic_search=include_semantic_search)


def workspace_deps(
    root: Path,
    *,
    mutate: bool = True,
    session_id: str | None = None,
    event_agent: str = "agent",
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
        event_agent=event_agent,
        acceptance_tests=acceptance_tests or (),
        tool_call_log=tool_call_log,
        task_plan=task_plan,
        require_full_coverage=require_full_coverage,
        baseline_paths=baseline_paths,
    )
