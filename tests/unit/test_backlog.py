from __future__ import annotations

from sprint_crew.orchestrator.backlog import sort_stories
from sprint_crew.schemas.backlog import BacklogPlan, BacklogStory, ProductBrief


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
