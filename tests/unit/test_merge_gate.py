from __future__ import annotations

from sprint_crew.orchestrator.merge_gate import review_accepted
from sprint_crew.schemas.change import ReviewFinding, ReviewOutcome


def test_review_accepted_passes_clean_review() -> None:
    review = ReviewOutcome(
        ticket_key="DEMO-1",
        passed=True,
        summary="ok",
        tests_passed=True,
        findings=[],
    )
    assert review_accepted(review) is True


def test_review_accepted_blocks_on_incomplete_coverage() -> None:
    review = ReviewOutcome(
        ticket_key="DEMO-1",
        passed=True,
        summary="ok",
        tests_passed=True,
        findings=[],
    )
    assert review_accepted(review, coverage_satisfied=False) is False


def test_review_accepted_blocks_on_blocker() -> None:
    review = ReviewOutcome(
        ticket_key="DEMO-1",
        passed=True,
        summary="issues",
        tests_passed=True,
        findings=[
            ReviewFinding(severity="blocker", message="broken"),
        ],
    )
    assert review_accepted(review) is False


def test_review_accepted_blocks_when_passed_false() -> None:
    review = ReviewOutcome(
        ticket_key="DEMO-1",
        passed=False,
        summary="no",
        tests_passed=True,
        findings=[],
    )
    assert review_accepted(review) is False


def test_review_accepted_blocks_when_tests_passed_false() -> None:
    review = ReviewOutcome(
        ticket_key="DEMO-1",
        passed=True,
        summary="ok",
        tests_passed=False,
        findings=[],
    )
    assert review_accepted(review) is False


def test_review_accepted_allows_warning_finding() -> None:
    review = ReviewOutcome(
        ticket_key="DEMO-1",
        passed=True,
        summary="ok",
        tests_passed=True,
        findings=[ReviewFinding(severity="warning", message="style")],
    )
    assert review_accepted(review) is True


def test_review_accepted_allows_nit_finding() -> None:
    review = ReviewOutcome(
        ticket_key="DEMO-1",
        passed=True,
        summary="ok",
        tests_passed=True,
        findings=[ReviewFinding(severity="nit", message="rename var")],
    )
    assert review_accepted(review) is True
