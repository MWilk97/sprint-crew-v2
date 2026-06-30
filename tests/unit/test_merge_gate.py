from __future__ import annotations

from sprint_crew.schemas.change import ReviewFinding, ReviewOutcome
from sprint_crew.orchestrator.merge_gate import review_accepted


def test_review_accepted_passes_clean_review() -> None:
    review = ReviewOutcome(
        ticket_key="DEMO-1",
        passed=True,
        summary="ok",
        tests_passed=True,
        findings=[],
    )
    assert review_accepted(review) is True


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
