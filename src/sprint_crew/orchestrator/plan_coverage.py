from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from sprint_crew.orchestrator.acceptance_failure import analyze_acceptance_output
from sprint_crew.paths import is_test_path, normalize_path, paths_from_diff
from sprint_crew.schemas.ticket import TaskPlan

_NOISE_PATH_RE = re.compile(r"__pycache__|pytest_cache|\.pyc$|\.(orig|rej)$")


def is_noise_path(path: str) -> bool:
    cleaned = path.replace("\\", "/")
    return bool(_NOISE_PATH_RE.search(cleaned))


def step_file_paths(plan: TaskPlan) -> set[str]:
    paths: set[str] = set()
    for step in plan.steps:
        for raw in step.files:
            normalized = normalize_path(raw)
            if normalized:
                paths.add(normalized)
    return paths


def expected_paths(plan: TaskPlan) -> set[str]:
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


def paths_from_patch(patch: str) -> list[str]:
    paths: list[str] = []
    for line in patch.splitlines():
        if not (line.startswith("+++ ") or line.startswith("--- ")):
            continue
        candidate = line[4:].strip()
        if candidate.startswith("b/"):
            candidate = candidate[2:]
        elif candidate.startswith("a/"):
            candidate = candidate[2:]
        if candidate and candidate != "/dev/null":
            paths.append(normalize_path(candidate))
    return paths


def check_mutation_allowed(
    path: str, plan: TaskPlan | None, *, tests_only: bool = False
) -> str | None:
    """Return an agent-facing error when a mutation is out of plan scope."""
    norm = normalize_path(path)
    if tests_only:
        if norm == "tests" or norm.startswith("tests/"):
            return None
        return f"Refused: Tester may only write under tests/: got {path!r}"

    out_of_scope = {normalize_path(raw) for raw in plan.out_of_scope if normalize_path(raw)}
    if norm in out_of_scope:
        return f"Refused: {norm} is out_of_scope in TaskPlan."

    allowed = frozenset(expected_paths(plan))
    if norm.startswith("src/") and norm not in allowed:
        return (
            f"Refused: {norm} is not in files_to_touch/steps. "
            "Edit only planned source files; use read_file to inspect others."
        )
    if norm.startswith("tests/") and norm not in allowed and not _plan_requires_new_tests(plan):
        return (
            f"Refused: {norm} is not listed in the TaskPlan. "
            "Add it to files_to_touch/steps or let the Tester handle tests."
        )
    return None


def check_patch_mutations_allowed(
    patch: str, plan: TaskPlan | None, *, tests_only: bool = False
) -> str | None:
    seen: set[str] = set()
    for raw_path in paths_from_patch(patch):
        if raw_path in seen:
            continue
        seen.add(raw_path)
        err = check_mutation_allowed(raw_path, plan, tests_only=tests_only)
        if err:
            return err
    return None


def _paths_from_porcelain(status_text: str) -> set[str]:
    paths: set[str] = set()
    for line in status_text.splitlines():
        if len(line) < 4:
            continue
        entry = line[3:].strip()
        if " -> " in entry:
            entry = entry.split(" -> ", 1)[1]
        if entry.startswith('"') and entry.endswith('"'):
            entry = entry[1:-1]
        normalized = normalize_path(entry)
        if normalized:
            paths.add(normalized)
    return paths


def _run_git(args: list[str], workspace_root: Path) -> str:
    proc = subprocess.run(
        ["git", *args],
        cwd=workspace_root,
        capture_output=True,
        text=True,
        check=False,
    )
    return (proc.stdout or "") + (proc.stderr or "")


def collect_changed_paths(workspace_root: Path) -> set[str]:
    """Return paths changed in the workspace (modified, staged, and untracked)."""
    root = workspace_root.resolve()
    paths: set[str] = set()

    diff_out = _run_git(["diff", "--name-only"], root)
    paths.update(normalize_path(line) for line in diff_out.splitlines() if line.strip())

    staged_out = _run_git(["diff", "--cached", "--name-only"], root)
    paths.update(normalize_path(line) for line in staged_out.splitlines() if line.strip())

    status_out = _run_git(["status", "--porcelain"], root)
    paths.update(_paths_from_porcelain(status_out))

    diff_text = _run_git(["diff"], root)
    paths.update(paths_from_diff(diff_text))

    return {path for path in paths if path and not is_noise_path(path)}


def _is_ignorable_missing(
    path: str,
    plan: TaskPlan,
    workspace_root: Path,
    *,
    baseline_paths: frozenset[str] | None = None,
) -> bool:
    """Paths that need not appear in git diff to satisfy coverage."""
    from sprint_crew.orchestrator.plan_validation import step_requires_test_edit

    normalized = normalize_path(path)

    if is_test_path(normalized):
        if (workspace_root / normalized).is_file() and not step_requires_test_edit(
            plan, normalized
        ):
            return True
        return False

    if normalized in step_file_paths(plan):
        return False
    if not (workspace_root / normalized).is_file():
        return False
    if baseline_paths is not None and normalized in baseline_paths:
        return True
    return False


def _plan_requires_new_tests(plan: TaskPlan) -> bool:
    for step in plan.steps:
        for path in step.files:
            normalized = normalize_path(path)
            if normalized.startswith("tests/") or "/tests/" in normalized:
                return True
    return any(normalize_path(path).startswith("tests/") for path in plan.files_to_touch)


def _allowed_unexpected(path: str, *, plan_requires_tests: bool) -> bool:
    if plan_requires_tests and (path.startswith("tests/") or "/tests/" in path):
        return True
    return False


@dataclass(frozen=True)
class PlanCoverageResult:
    missing: list[str]
    unexpected: list[str]
    out_of_scope_hits: list[str]
    blocking_unexpected: list[str] = field(default_factory=list)
    phantom_paths: list[str] = field(default_factory=list)
    satisfied: bool = False


def validate_plan_coverage(
    plan: TaskPlan,
    workspace_root: Path,
    *,
    baseline_paths: frozenset[str] | None = None,
) -> PlanCoverageResult:
    expected = expected_paths(plan)
    changed = collect_changed_paths(workspace_root)
    plan_requires_tests = _plan_requires_new_tests(plan)

    out_of_scope = {normalize_path(path) for path in plan.out_of_scope if normalize_path(path)}
    out_of_scope_hits = sorted(path for path in changed if path in out_of_scope)

    phantom_paths: list[str] = []
    if expected and baseline_paths is not None:
        from sprint_crew.orchestrator.plan_validation import is_allowed_plan_path

        phantom_paths = sorted(
            path
            for path in expected
            if path not in changed
            and path not in baseline_paths
            and not is_allowed_plan_path(workspace_root, path, plan, baseline_paths=baseline_paths)
        )

    if expected:
        missing = sorted(
            path
            for path in expected
            if path not in changed
            and path not in phantom_paths
            and not _is_ignorable_missing(path, plan, workspace_root, baseline_paths=baseline_paths)
        )
        unexpected = sorted(
            path
            for path in changed
            if path not in expected
            and not is_noise_path(path)
            and not _allowed_unexpected(path, plan_requires_tests=plan_requires_tests)
        )
        blocking_unexpected = sorted(
            path for path in unexpected if path.startswith("src/") and path not in out_of_scope
        )
        # blocking_unexpected is advisory only: green tests + passed review +
        # manual merge gate already guard against real scope creep. Explicit
        # out_of_scope violations still block via out_of_scope_hits.
        satisfied = not out_of_scope_hits and not missing
    else:
        missing = []
        unexpected = sorted(
            path
            for path in changed
            if not is_noise_path(path)
            and not _allowed_unexpected(path, plan_requires_tests=plan_requires_tests)
        )
        blocking_unexpected = sorted(
            path for path in unexpected if path.startswith("src/") and path not in out_of_scope
        )
        satisfied = bool(changed) and not out_of_scope_hits

    return PlanCoverageResult(
        missing=missing,
        unexpected=unexpected,
        out_of_scope_hits=out_of_scope_hits,
        blocking_unexpected=blocking_unexpected,
        phantom_paths=phantom_paths,
        satisfied=satisfied,
    )


def continuation_makes_sense(
    coverage: PlanCoverageResult,
    workspace_root: Path,
    plan: TaskPlan,
) -> bool:
    """False when Coder cannot plausibly fix coverage (e.g. phantom paths in plan)."""
    if coverage.phantom_paths:
        return False
    if coverage.satisfied or not coverage.missing:
        return False
    fixable = [
        path
        for path in coverage.missing
        if not _is_ignorable_missing(path, plan, workspace_root)
        and (not (workspace_root / path).is_file() or path in step_file_paths(plan))
    ]
    return bool(fixable)


def coverage_improved(before: PlanCoverageResult, after: PlanCoverageResult) -> bool:
    if after.satisfied:
        return True
    before_pressure = len(before.missing) + len(before.out_of_scope_hits)
    after_pressure = len(after.missing) + len(after.out_of_scope_hits)
    return after_pressure < before_pressure


def should_invoke_tester(
    plan: TaskPlan,
    workspace_root: Path,
    *,
    acceptance_green: bool,
    acceptance_output: str = "",
) -> bool:
    """True when Tester should run: AC red, no tests/ in plan, source changed without tests/ diff."""
    if acceptance_green:
        return False
    if _plan_requires_new_tests(plan):
        return False
    changed = collect_changed_paths(workspace_root)
    source_changed = {path for path in changed if not path.startswith("tests/")}
    tests_changed = {path for path in changed if path.startswith("tests/")}
    if not (bool(source_changed) and not tests_changed):
        return False
    if acceptance_output.strip():
        analysis = analyze_acceptance_output(acceptance_output)
        if not analysis.tester_can_help:
            return False
    return True
