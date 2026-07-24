from __future__ import annotations

import sys
from pathlib import Path

from sprint_crew.orchestrator.acceptance_failure import (
    AcceptanceFailureAnalysis,
    analyze_acceptance_output,
)
from sprint_crew.orchestrator.plan_coverage import PlanCoverageResult
from sprint_crew.schemas.change import ReviewOutcome

_PLAN_KEYWORDS = (
    "out of scope",
    "wrong file",
    "missing step",
    "task plan",
    "taskplan",
    "replan",
    "wrong files",
    "scope creep",
    "not in plan",
)


def resolve_retry_scope_from_coverage(
    coverage: PlanCoverageResult,
    *,
    workspace_root: Path | None = None,
    review_passed: bool = False,
) -> str | None:
    if coverage.phantom_paths:
        return "plan"
    if coverage.satisfied:
        return None
    if coverage.out_of_scope_hits:
        return "plan"
    if review_passed and coverage.unexpected:
        return "code"
    if coverage.missing:
        if workspace_root is None:
            return None
        if all((workspace_root / path).is_file() for path in coverage.missing):
            return "plan"
        return "code"
    return None


def resolve_retry_scope(
    review: ReviewOutcome,
    *,
    coverage: PlanCoverageResult | None = None,
    workspace_root: Path | None = None,
) -> str:
    """Return 'plan' or 'code' for retry routing after a rejected review."""
    if coverage is not None:
        coverage_scope = resolve_retry_scope_from_coverage(
            coverage,
            workspace_root=workspace_root,
            review_passed=review.passed,
        )
        if coverage_scope is not None:
            return coverage_scope
    if review.passed:
        return "code"
    if review.retry_scope == "plan":
        return "plan"
    text = " ".join(
        [
            review.summary.lower(),
            *[f.message.lower() for f in review.findings],
        ]
    )
    if any(keyword in text for keyword in _PLAN_KEYWORDS):
        return "plan"
    return "code"


def _failure_analysis_from_state(
    raw: dict[str, object] | None,
) -> AcceptanceFailureAnalysis | None:
    if not raw or not raw.get("kind") or raw.get("kind") == "none":
        return None
    return AcceptanceFailureAnalysis(
        kind=raw["kind"],  # type: ignore[arg-type]
        tester_can_help=bool(raw.get("tester_can_help", False)),
        source_paths=tuple(str(p) for p in raw.get("source_paths", [])),  # type: ignore[union-attr]
        test_paths=tuple(str(p) for p in raw.get("test_paths", [])),  # type: ignore[union-attr]
        summary=str(raw.get("summary", "")),
        detail_excerpt=str(raw.get("detail_excerpt", "")),
    )


def _stdlib_shadow_packages(source_paths: tuple[str, ...]) -> list[str]:
    """Local top-level packages whose name collides with a Python stdlib module.

    A build failure plus a package like ``src/platform/`` that shadows stdlib
    ``platform`` is the classic import-resolution trap; naming it explicitly gives
    the Coder a concrete lead instead of a generic 'check imports' nudge.
    """
    stdlib = getattr(sys, "stdlib_module_names", frozenset())
    roots = {"src", "tests", "test", "lib"}
    shadows: set[str] = set()
    for raw in source_paths:
        segments = raw.replace("\\", "/").split("/")[:-1]  # drop the filename
        for seg in segments:
            if seg and seg not in roots and seg in stdlib:
                shadows.add(seg)
    return sorted(shadows)


def _format_failure_feedback(analysis: AcceptanceFailureAnalysis) -> list[str]:
    if analysis.kind == "none":
        return []
    lines: list[str] = []
    if not analysis.tester_can_help:
        label = analysis.kind.upper().replace("_", " ")
        lines.append(f"SOURCE_BUILD_FAILURE ({label}):")
        lines.append(analysis.summary)
        if analysis.source_paths:
            lines.append("Affected source files: " + ", ".join(analysis.source_paths))
        shadows = _stdlib_shadow_packages(analysis.source_paths)
        if shadows:
            named = ", ".join(f"'{name}'" for name in shadows)
            lines.append(
                f"STDLIB SHADOW DETECTED: local package(s) {named} shadow a Python "
                "stdlib module of the same name, so imports resolve to the wrong module. "
                "Fix import resolution (absolute/relative imports, package __init__, or "
                "sys.path) — do NOT rename the stdlib usage."
            )
        lines.append(
            "The Tester agent cannot modify src/ — fix imports/syntax in source files first."
        )
        lines.append(
            "Suggested focus: check import paths, stdlib name collisions, missing modules."
        )
    elif analysis.kind == "assertion_failure" and analysis.test_paths:
        lines.append("Assertion failures in: " + ", ".join(analysis.test_paths))
    if analysis.detail_excerpt.strip():
        lines.append("Error excerpt:")
        lines.append(analysis.detail_excerpt.strip())
    return lines


def resolve_failure_feedback(
    *,
    test_output: str = "",
    acceptance_failure: dict[str, object] | None = None,
    test_additions: dict[str, object] | None = None,
) -> tuple[AcceptanceFailureAnalysis | None, str]:
    failure_analysis: AcceptanceFailureAnalysis | None = None
    if test_output.strip():
        analyzed = analyze_acceptance_output(test_output)
        if analyzed.kind != "none":
            failure_analysis = analyzed
    if failure_analysis is None:
        failure_analysis = _failure_analysis_from_state(acceptance_failure)
    bugs_observed = ""
    if test_additions:
        bugs_observed = str(test_additions.get("bugs_observed", "") or "")
    return failure_analysis, bugs_observed


def format_review_feedback(
    review: ReviewOutcome,
    *,
    workspace_diff: str = "",
    test_output: str = "",
    coverage: PlanCoverageResult | None = None,
    workspace_root: Path | None = None,
    failure_analysis: AcceptanceFailureAnalysis | None = None,
    bugs_observed: str = "",
) -> str:
    lines: list[str] = []
    if failure_analysis is not None:
        lines.extend(_format_failure_feedback(failure_analysis))
    if bugs_observed.strip():
        lines.append("Tester observations (bugs in source — fix in src/, not tests/):")
        lines.append(bugs_observed.strip())
    lines.append(f"Review summary: {review.summary}")
    if coverage is not None and not coverage.satisfied:
        lines.append("Merge gate blocked: plan coverage incomplete.")
        if coverage.missing:
            lines.append("Coverage missing: " + ", ".join(coverage.missing))
        if coverage.unexpected:
            lines.append("Coverage unexpected: " + ", ".join(coverage.unexpected))
        if coverage.blocking_unexpected:
            lines.append(
                "Coverage note (advisory): src files changed outside plan — "
                "verify they are in scope, or add them to files_to_touch: "
                + ", ".join(coverage.blocking_unexpected)
            )
        if coverage.phantom_paths:
            lines.append("Coverage phantom_paths: " + ", ".join(coverage.phantom_paths))
        if coverage.out_of_scope_hits:
            lines.append("Coverage out_of_scope_hits: " + ", ".join(coverage.out_of_scope_hits))
        if workspace_root is not None and coverage.missing:
            baseline_test_missing = sorted(
                path
                for path in coverage.missing
                if path.startswith("tests/") and (workspace_root / path).is_file()
            )
            if baseline_test_missing:
                lines.append(
                    "Remove unchanged baseline test paths from files_to_touch/steps; "
                    "verification is via acceptance_tests: " + ", ".join(baseline_test_missing)
                )
    if not review.tests_passed:
        lines.append("Acceptance tests FAILED when re-run by the Reviewer.")
        if review.tests_run:
            lines.append(
                "Make these failing acceptance tests pass FIRST: " + "; ".join(review.tests_run)
            )
    if not review.passed:
        lines.append("Review marked as not passed.")
    blockers = [f for f in review.findings if f.severity == "blocker"]
    warnings = [f for f in review.findings if f.severity == "warning"]
    for finding in blockers:
        loc = ""
        if finding.file:
            loc = f"{finding.file}"
            if finding.line:
                loc += f":{finding.line}"
            loc = f"{loc} — "
        lines.append(f"[blocker] {loc}{finding.message}")
    for finding in warnings[:5]:
        loc = f"{finding.file}: " if finding.file else ""
        lines.append(f"[warning] {loc}{finding.message}")
    if review.tests_run:
        lines.append("Tests run: " + "; ".join(review.tests_run))
    if test_output.strip():
        if failure_analysis is None or not failure_analysis.detail_excerpt.strip():
            lines.append("Latest test output:")
            lines.append(test_output.strip()[-4000:])
    if workspace_diff.strip():
        lines.append("Workspace diff (truncated):")
        lines.append(workspace_diff.strip()[:8000])
    return "\n".join(lines)
