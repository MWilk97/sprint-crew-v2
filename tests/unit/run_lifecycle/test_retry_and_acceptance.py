from __future__ import annotations

from pathlib import Path

from tests.helpers.acceptance_output import SCRUM3_COLLECTION

from sprint_crew.orchestrator.acceptance_failure import analyze_acceptance_output
from sprint_crew.orchestrator.acceptance_tests import tool_log_shows_passing_acceptance
from sprint_crew.orchestrator.plan_coverage import PlanCoverageResult
from sprint_crew.orchestrator.retry import (
    escalate_scope_for_build_failure,
    format_review_feedback,
    resolve_retry_scope,
    resolve_retry_scope_from_coverage,
)
from sprint_crew.schemas.change import ReviewFinding, ReviewOutcome


def test_collection_error_in_source_disables_tester() -> None:
    analysis = analyze_acceptance_output(SCRUM3_COLLECTION)
    assert analysis.kind == "collection_error"
    assert analysis.tester_can_help is False
    assert "src/api/routes.py" in analysis.source_paths


def test_import_error_only_in_tests_allows_tester() -> None:
    output = """
$ pytest tests/test_x.py -q
exit_code=2
tests/test_x.py:3: in <module>
    from missing_dep import helper
E   ModuleNotFoundError: No module named 'missing_dep'
"""
    analysis = analyze_acceptance_output(output)
    assert analysis.kind == "import_error"
    assert analysis.tester_can_help is True
    assert analysis.test_paths == ("tests/test_x.py",)


def test_assertion_failure_allows_tester() -> None:
    output = """
$ pytest tests/test_greeter.py -q
exit_code=1
FAILED tests/test_greeter.py::test_hello - AssertionError: assert 'hi' == 'hello'
"""
    analysis = analyze_acceptance_output(output)
    assert analysis.kind == "assertion_failure"
    assert analysis.tester_can_help is True


def test_green_output_is_none() -> None:
    output = """
$ pytest -q
exit_code=0
3 passed
"""
    analysis = analyze_acceptance_output(output)
    assert analysis.kind == "none"
    assert analysis.tester_can_help is False


def test_detail_excerpt_is_bounded() -> None:
    analysis = analyze_acceptance_output(SCRUM3_COLLECTION)
    assert "ModuleNotFoundError" in analysis.detail_excerpt
    assert analysis.detail_excerpt.count("\n") <= 30


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


def test_stdlib_shadow_named_in_build_failure_feedback() -> None:
    # A collection error whose source path is a local package that shadows a
    # stdlib module (``platform``) should surface an explicit shadow hint.
    test_output = (
        "exit_code=2\n"
        "ERROR collecting tests/test_notify_routes.py\n"
        'File "src/platform/config.py", line 3, in <module>\n'
        "ImportError: cannot import name 'system' from 'platform'\n"
        "!!!!!! Interrupted: 1 error during collection !!!!!!"
    )
    analysis = analyze_acceptance_output(test_output)
    outcome = ReviewOutcome(
        ticket_key="SCRUM-3",
        passed=False,
        summary="build failure",
        tests_passed=False,
    )
    feedback = format_review_feedback(
        outcome,
        test_output=test_output,
        failure_analysis=analysis,
    )
    assert "POSSIBLE STDLIB SHADOW" in feedback
    assert "'platform'" in feedback


def test_no_shadow_hint_when_no_stdlib_collision() -> None:
    test_output = (
        "exit_code=2\n"
        "ERROR collecting tests/test_notify_routes.py\n"
        'File "src/messaging/queue_worker.py", line 3, in <module>\n'
        "ImportError: cannot import name 'Ferry' from 'messaging.ferry'\n"
        "!!!!!! Interrupted: 1 error during collection !!!!!!"
    )
    analysis = analyze_acceptance_output(test_output)
    outcome = ReviewOutcome(
        ticket_key="SCRUM-3",
        passed=False,
        summary="build failure",
        tests_passed=False,
    )
    feedback = format_review_feedback(
        outcome,
        test_output=test_output,
        failure_analysis=analysis,
    )
    assert "STDLIB SHADOW DETECTED" not in feedback


def test_stdlib_paths_never_reach_the_shadow_hint() -> None:
    """A traceback frame from the interpreter is not a local package.

    This is the shape that told a Coder its "local package 'importlib'" was the problem
    while the real collision (``src/platform/``) went unmentioned for two hours.
    """
    test_output = (
        "exit_code=2\n"
        "ERROR collecting tests/test_notify_routes.py\n"
        'File "/usr/lib/python3.12/importlib/__init__.py", line 90, in import_module\n'
        'File "src/api/routes.py", line 12, in <module>\n'
        "ModuleNotFoundError: No module named 'platform.config'; 'platform' is not a package\n"
    )
    analysis = analyze_acceptance_output(test_output)
    assert "src/api/routes.py" in analysis.source_paths
    assert not any("importlib" in path for path in analysis.source_paths)

    outcome = ReviewOutcome(
        ticket_key="SCRUM-3", passed=False, summary="build failure", tests_passed=False
    )
    feedback = format_review_feedback(outcome, test_output=test_output, failure_analysis=analysis)
    assert "'importlib'" not in feedback
    # The interpreter's own message names the real culprit, so it must lead the feedback.
    assert feedback.index("Error output (authoritative") < feedback.index("Review summary")
    assert "'platform' is not a package" in feedback


def test_build_failure_escalates_a_code_retry_to_replanning() -> None:
    build_failure = analyze_acceptance_output(
        "exit_code=2\n"
        "ERROR collecting tests/test_x.py\n"
        'File "src/api/routes.py", line 12, in <module>\n'
        "ModuleNotFoundError: No module named 'platform.config'\n"
    )
    assert build_failure.tester_can_help is False
    assert escalate_scope_for_build_failure("code", build_failure) == "plan"
    # A plan-scoped retry and a tester-fixable failure are both left alone.
    assert escalate_scope_for_build_failure("plan", build_failure) == "plan"
    assert escalate_scope_for_build_failure("code", None) == "code"


def test_unverified_green_claim_is_not_supported_by_an_empty_tool_log(tmp_path) -> None:
    """A Coder claiming green must have a passing run somewhere on record.

    The orchestrator logs every run_command with its ok flag, so the claim is checkable
    against what the agent actually observed rather than taken on trust.
    """
    commands = ["pytest -q tests/test_notify_routes.py"]
    assert tool_log_shows_passing_acceptance([], commands, tmp_path) is False

    only_errors = [
        {
            "tool": "run_command",
            "args": {"command": "pytest -q tests/test_notify_routes.py"},
            "ok": False,
        }
    ]
    assert tool_log_shows_passing_acceptance(only_errors, commands, tmp_path) is False

    passed = [
        {
            "tool": "run_command",
            "args": {"command": "pytest -q tests/test_notify_routes.py"},
            "ok": True,
        }
    ]
    assert tool_log_shows_passing_acceptance(passed, commands, tmp_path) is True


def test_a_passing_unrelated_command_does_not_vouch_for_the_acceptance_tests(tmp_path) -> None:
    log = [
        {"tool": "run_command", "args": {"command": "pytest -q tests/test_other.py"}, "ok": True}
    ]
    assert (
        tool_log_shows_passing_acceptance(log, ["pytest -q tests/test_notify.py"], tmp_path)
        is False
    )
