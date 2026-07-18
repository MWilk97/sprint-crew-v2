from __future__ import annotations

from pathlib import Path

from sprint_crew.orchestrator.acceptance_failure import analyze_acceptance_output
from sprint_crew.orchestrator.plan_coverage import PlanCoverageResult
from sprint_crew.orchestrator.retry import (
    format_review_feedback,
    resolve_retry_scope,
    resolve_retry_scope_from_coverage,
)
from sprint_crew.schemas.change import ReviewFinding, ReviewOutcome


def test_resolve_retry_scope_from_coverage_phantom_paths() -> None:
    coverage = PlanCoverageResult(
        missing=[],
        unexpected=[],
        out_of_scope_hits=[],
        phantom_paths=["src/ferry/layer.py"],
        satisfied=False,
    )
    assert resolve_retry_scope_from_coverage(coverage) == "plan"


def test_resolve_retry_scope_from_coverage_none_when_satisfied() -> None:
    coverage = PlanCoverageResult(
        missing=["a.py"],
        unexpected=[],
        out_of_scope_hits=[],
        satisfied=True,
    )
    assert resolve_retry_scope_from_coverage(coverage) is None


def test_resolve_retry_scope_from_coverage_missing_all_exist(tmp_path: Path) -> None:
    existing = tmp_path / "worker.py"
    existing.write_text("stub\n", encoding="utf-8")
    coverage = PlanCoverageResult(
        missing=["worker.py"],
        unexpected=[],
        out_of_scope_hits=[],
        satisfied=False,
    )
    assert resolve_retry_scope_from_coverage(coverage, workspace_root=tmp_path) == "plan"


def test_resolve_retry_scope_from_coverage_missing_gap_is_code(tmp_path: Path) -> None:
    coverage = PlanCoverageResult(
        missing=["missing.py"],
        unexpected=[],
        out_of_scope_hits=[],
        satisfied=False,
    )
    assert resolve_retry_scope_from_coverage(coverage, workspace_root=tmp_path) == "code"


def test_resolve_retry_scope_from_coverage_unexpected_when_review_passed() -> None:
    coverage = PlanCoverageResult(
        missing=[],
        unexpected=["src/messaging/retry_policy.py"],
        out_of_scope_hits=[],
        satisfied=False,
    )
    assert resolve_retry_scope_from_coverage(coverage, review_passed=True) == "code"


def test_resolve_retry_scope_coverage_missing_all_exist_overrides_passed_review(
    tmp_path: Path,
) -> None:
    (tmp_path / "worker.py").write_text("x\n", encoding="utf-8")
    review = ReviewOutcome(
        ticket_key="DEMO-1",
        passed=True,
        summary="ok",
        tests_passed=True,
        retry_scope="code",
        findings=[],
        tests_run=[],
    )
    coverage = PlanCoverageResult(
        missing=["worker.py"],
        unexpected=[],
        out_of_scope_hits=[],
        satisfied=False,
    )
    assert resolve_retry_scope(review, coverage=coverage, workspace_root=tmp_path) == "plan"


def test_resolve_retry_scope_from_coverage_out_of_scope_hits() -> None:
    coverage = PlanCoverageResult(
        missing=[],
        unexpected=[],
        out_of_scope_hits=["src/messaging/ferry.py", "src/storage/sqlite_repo.py"],
        satisfied=False,
    )
    assert resolve_retry_scope_from_coverage(coverage, review_passed=True) == "plan"


def test_resolve_retry_scope_out_of_scope_hits_overrides_passed_review() -> None:
    review = ReviewOutcome(
        ticket_key="DEMO-1",
        passed=True,
        summary="ok",
        tests_passed=True,
        retry_scope="code",
        findings=[],
        tests_run=[],
    )
    coverage = PlanCoverageResult(
        missing=[],
        unexpected=[],
        out_of_scope_hits=["src/messaging/ferry.py"],
        satisfied=False,
    )
    assert resolve_retry_scope(review, coverage=coverage) == "plan"


def test_resolve_retry_scope_from_coverage_blocking_unexpected_advisory() -> None:
    # blocking_unexpected is advisory (PR-A): an unplanned src edit alone leaves
    # coverage satisfied, so no retry is triggered.
    coverage = PlanCoverageResult(
        missing=[],
        unexpected=["src/storage/sqlite_repo.py"],
        out_of_scope_hits=[],
        blocking_unexpected=["src/storage/sqlite_repo.py"],
        satisfied=True,
    )
    assert resolve_retry_scope_from_coverage(coverage, review_passed=True) is None


def test_format_review_feedback_includes_blockers() -> None:
    review = ReviewOutcome(
        ticket_key="DEMO-1",
        passed=False,
        summary="Needs fixes",
        tests_passed=False,
        retry_scope="code",
        findings=[
            ReviewFinding(severity="blocker", file="greeter.py", line=3, message="missing hello()"),
        ],
        tests_run=["pytest -q"],
    )
    feedback = format_review_feedback(review)
    assert "blocker" in feedback
    assert "missing hello()" in feedback
    assert "FAILED" in feedback


def test_format_review_feedback_names_failing_tests_first() -> None:
    review = ReviewOutcome(
        ticket_key="DEMO-3",
        passed=False,
        summary="tests red",
        tests_passed=False,
        retry_scope="code",
        findings=[],
        tests_run=["pytest -q tests/test_routes.py"],
    )
    feedback = format_review_feedback(review)
    assert "Make these failing acceptance tests pass FIRST" in feedback
    assert "tests/test_routes.py" in feedback


def test_format_review_feedback_includes_coverage() -> None:
    review = ReviewOutcome(
        ticket_key="DEMO-1",
        passed=True,
        summary="All tests pass",
        tests_passed=True,
        retry_scope="code",
        findings=[],
        tests_run=["pytest -q"],
    )
    coverage = PlanCoverageResult(
        missing=["src/worker.py"],
        unexpected=["src/other.py"],
        out_of_scope_hits=[],
        satisfied=False,
    )
    feedback = format_review_feedback(review, coverage=coverage)
    assert "Merge gate blocked" in feedback
    assert "src/worker.py" in feedback
    assert "src/other.py" in feedback


def test_format_review_feedback_hints_baseline_tests(tmp_path: Path) -> None:
    test_file = tmp_path / "tests" / "test_ferry_retry.py"
    test_file.parent.mkdir(parents=True)
    test_file.write_text("pass\n", encoding="utf-8")
    review = ReviewOutcome(
        ticket_key="SCRUM-2",
        passed=True,
        summary="ok",
        tests_passed=True,
        retry_scope="plan",
        findings=[],
        tests_run=["pytest tests/test_ferry_retry.py -q"],
    )
    coverage = PlanCoverageResult(
        missing=["tests/test_ferry_retry.py"],
        unexpected=[],
        out_of_scope_hits=[],
        satisfied=False,
    )
    feedback = format_review_feedback(review, coverage=coverage, workspace_root=tmp_path)
    assert "baseline test paths" in feedback
    assert "tests/test_ferry_retry.py" in feedback


def test_format_review_feedback_includes_source_build_failure() -> None:
    review = ReviewOutcome(
        ticket_key="SCRUM-3",
        passed=False,
        summary="tests red",
        tests_passed=False,
        retry_scope="code",
        findings=[],
        tests_run=["pytest tests/test_notify_routes.py -q"],
    )
    analysis = analyze_acceptance_output(
        """
$ pytest tests/test_notify_routes.py -q
exit_code=2
src/api/routes.py:11: in <module>
    from platform.config import default_config
E   ModuleNotFoundError: No module named 'platform.config'
"""
    )
    feedback = format_review_feedback(review, failure_analysis=analysis)
    assert "SOURCE_BUILD_FAILURE" in feedback
    assert "src/api/routes.py" in feedback
    assert "Tester agent cannot modify src/" in feedback


def test_format_review_feedback_includes_bugs_observed() -> None:
    review = ReviewOutcome(
        ticket_key="DEMO-1",
        passed=False,
        summary="needs fix",
        tests_passed=False,
        retry_scope="code",
        findings=[],
        tests_run=["pytest -q"],
    )
    feedback = format_review_feedback(
        review,
        bugs_observed="Import broken in src/api/routes.py:11 — Coder must fix src/",
    )
    assert "Tester observations" in feedback
    assert "src/api/routes.py" in feedback


def test_format_review_feedback_includes_diff_and_tests() -> None:
    review = ReviewOutcome(
        ticket_key="DEMO-1",
        passed=False,
        summary="Fix hello() return value",
        tests_passed=False,
        tests_run=["pytest -q"],
    )
    feedback = format_review_feedback(
        review,
        workspace_diff="diff snippet",
        test_output="stderr: AssertionError",
    )
    assert "Workspace diff" in feedback
    assert "Latest test output" in feedback
    assert "AssertionError" in feedback
