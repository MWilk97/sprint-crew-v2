from __future__ import annotations

import re
from pathlib import Path

from sprint_crew.orchestrator.plan_coverage import normalize_path
from sprint_crew.schemas.ticket import TaskPlan
from sprint_crew.vector.chunker import INDEXABLE_SUFFIXES
from sprint_crew.vector.chunker import _should_skip_path as _chunker_should_skip_path

_NEW_FILE_STEP_RE = re.compile(
    r"\b(?:create|new\s+file|add\s+file|add\s+new)\b",
    re.IGNORECASE,
)


class PlanPathValidationError(ValueError):
    def __init__(self, phantom_paths: list[str]) -> None:
        self.phantom_paths = sorted(phantom_paths)
        paths_text = ", ".join(self.phantom_paths)
        super().__init__(f"TaskPlan references paths that do not exist: {paths_text}")


class PlanScopeValidationError(ValueError):
    def __init__(self, conflicts: list[str]) -> None:
        self.conflicts = sorted(conflicts)
        paths_text = ", ".join(self.conflicts)
        super().__init__(
            f"TaskPlan lists paths in both files_to_touch/steps and out_of_scope: {paths_text}"
        )


def _is_indexable_file(rel: str) -> bool:
    suffix = Path(rel).suffix.lower()
    return suffix in INDEXABLE_SUFFIXES and not _chunker_should_skip_path(Path(rel))


def snapshot_baseline_paths(workspace: Path) -> frozenset[str]:
    """All indexable files in the workspace at session start."""
    root = workspace.resolve()
    paths: set[str] = set()
    for file_path in root.rglob("*"):
        if not file_path.is_file():
            continue
        try:
            rel = file_path.relative_to(root).as_posix()
        except ValueError:
            continue
        if _is_indexable_file(rel):
            paths.add(rel)
    return frozenset(paths)


def _step_allows_new_file(plan: TaskPlan, normalized: str) -> bool:
    for step in plan.steps:
        if normalized not in {normalize_path(raw) for raw in step.files}:
            continue
        if _NEW_FILE_STEP_RE.search(step.description):
            return True
    return False


def step_requires_test_edit(plan: TaskPlan, path: str) -> bool:
    """True when a tests/ path is a planned new-file edit (not AC-only verification)."""
    normalized = normalize_path(path)
    if not (normalized.startswith("tests/") or "/tests/" in normalized):
        return False
    return _step_allows_new_file(plan, normalized)


def _is_allowed_new_path(
    workspace: Path,
    normalized: str,
    plan: TaskPlan,
    *,
    baseline_paths: frozenset[str],
) -> bool:
    if normalized in baseline_paths:
        return True
    if (workspace / normalized).is_file():
        return True
    if normalized.startswith("tests/") or "/tests/" in normalized:
        parent = (workspace / normalized).parent
        if parent.is_dir():
            return True
    if _step_allows_new_file(plan, normalized):
        return True
    return False


def is_allowed_plan_path(
    workspace: Path,
    path: str,
    plan: TaskPlan,
    *,
    baseline_paths: frozenset[str],
) -> bool:
    normalized = normalize_path(path)
    if not normalized:
        return True
    return _is_allowed_new_path(workspace, normalized, plan, baseline_paths=baseline_paths)


def _all_planned_paths(plan: TaskPlan) -> set[str]:
    paths: set[str] = set()
    for raw in plan.files_to_touch:
        normalized = normalize_path(raw)
        if normalized:
            paths.add(normalized)
    for step in plan.steps:
        for raw in step.files:
            normalized = normalize_path(raw)
            if normalized:
                paths.add(normalized)
    return paths


def validate_plan_scope_conflicts(plan: TaskPlan) -> None:
    """Reject plans where planned edits overlap out_of_scope."""
    planned = _all_planned_paths(plan)
    out_of_scope = {normalize_path(path) for path in plan.out_of_scope if normalize_path(path)}
    conflicts = sorted(path for path in planned if path in out_of_scope)
    if conflicts:
        raise PlanScopeValidationError(conflicts)


def validate_plan_paths_exist(
    workspace: Path,
    plan: TaskPlan,
    *,
    baseline_paths: frozenset[str] | None = None,
) -> None:
    """Reject TaskPlan paths that do not exist and are not allowed new files."""
    root = workspace.resolve()
    baseline = baseline_paths if baseline_paths is not None else snapshot_baseline_paths(root)
    phantom: list[str] = []
    for path in sorted(_all_planned_paths(plan)):
        if not _is_allowed_new_path(root, path, plan, baseline_paths=baseline):
            phantom.append(path)
    if phantom:
        raise PlanPathValidationError(phantom)
