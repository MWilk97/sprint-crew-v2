from __future__ import annotations

from sprint_crew.schemas.change import ReviewOutcome


def review_accepted(
    review: ReviewOutcome,
    *,
    coverage_satisfied: bool = True,
) -> bool:
    if not coverage_satisfied:
        return False
    if not review.passed:
        return False
    if not review.tests_passed:
        return False
    return not any(f.severity == "blocker" for f in review.findings)
