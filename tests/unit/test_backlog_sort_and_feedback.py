from __future__ import annotations

from sprint_crew.orchestrator.backlog import sort_stories
from sprint_crew.orchestrator.retry import format_review_feedback
from sprint_crew.schemas.backlog import BacklogPlan, BacklogStory, ProductBrief
from sprint_crew.schemas.change import ReviewOutcome


def _story(key: str, *, depends_on: list[str] | None = None) -> BacklogStory:
    return BacklogStory(
        key=key,
        summary=f"Story {key}",
        depends_on=depends_on or [],
    )


def test_sort_stories_respects_dependencies() -> None:
    plan = BacklogPlan(
        product_brief=ProductBrief(title="T", summary="S"),
        stories=[
            _story("B", depends_on=["A"]),
            _story("A"),
            _story("C", depends_on=["B"]),
        ],
        recommended_first="A",
    )
    ordered = sort_stories(plan)
    assert [story.key for story in ordered] == ["A", "B", "C"]


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
