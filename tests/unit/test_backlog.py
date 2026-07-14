from __future__ import annotations

import subprocess

import pytest

from sprint_crew.config import get_settings
from sprint_crew.orchestrator.backlog import normalize_backlog_plan, sort_stories
from sprint_crew.orchestrator.complexity import (
    PromptComplexity,
    assess_prompt_complexity,
    assess_ticket_complexity,
    tech_lead_mode,
)
from sprint_crew.orchestrator.session import prepare_chained_workspace
from sprint_crew.orchestrator.template_plan import work_lane_required_for_ticket
from sprint_crew.schemas.backlog import BacklogPlan, BacklogStory, ProductBrief
from sprint_crew.schemas.ticket import JiraTicket


def test_assess_prompt_complexity_trivial() -> None:
    prompt = "Add a hello() function to greeter.py that returns 'hello' and cover it with pytest."
    assert assess_prompt_complexity(prompt) == PromptComplexity.TRIVIAL


def test_assess_prompt_complexity_complex() -> None:
    prompt = (
        "Build a REST API for task management with CRUD endpoints, SQLite storage, "
        "input validation, pytest coverage, and OpenAPI docs. Split into shippable stories."
    )
    assert assess_prompt_complexity(prompt) == PromptComplexity.COMPLEX


def test_assess_ticket_complexity_epic_is_complex() -> None:
    ticket = JiraTicket(
        key="DEMO-2",
        summary="Build REST API for tasks",
        description="CRUD endpoints with SQLite and auth middleware.",
        status="To Do",
        issue_type="Epic",
        acceptance_criteria="All endpoints covered by integration tests",
    )
    assert assess_ticket_complexity(ticket) == PromptComplexity.COMPLEX
    assert tech_lead_mode(ticket) == "tool_loop"


def test_assess_ticket_complexity_e2e_greeter_summary_not_complex() -> None:
    ticket = JiraTicket(
        key="SCRUM-1",
        summary="[sprint-crew-test] api greeter ship_live greeter",
        description="Implement hello() returning 'hello' and ensure pytest passes.",
        status="To Do",
        issue_type="Story",
        acceptance_criteria="pytest -q tests/test_greeter.py passes",
    )
    assert assess_ticket_complexity(ticket) != PromptComplexity.COMPLEX


def test_work_lane_not_required_for_e2e_greeter_ticket() -> None:
    ticket = JiraTicket(
        key="SCRUM-1",
        summary="[sprint-crew-test] api greeter ship_live greeter",
        description="Implement hello() returning 'hello' and ensure pytest passes.",
        status="To Do",
        issue_type="Story",
        acceptance_criteria="pytest -q tests/test_greeter.py passes",
    )
    assert work_lane_required_for_ticket(ticket) is False


def test_assess_ticket_complexity_ship_live_email_is_simple() -> None:
    ticket = JiraTicket(
        key="DEMO-2",
        summary="Add validate_email() email ship live",
        description=(
            "Implement validate_email(address) in validators.py returning True for simple "
            "addresses with @ and a domain, False otherwise. Ensure pytest passes."
        ),
        status="To Do",
        issue_type="Story",
        acceptance_criteria="pytest -q tests/test_validators.py passes",
    )
    assert assess_ticket_complexity(ticket) != PromptComplexity.COMPLEX
    assert tech_lead_mode(ticket) == "static"


def test_assess_ticket_complexity_greeter_story_not_complex() -> None:
    ticket = JiraTicket(
        key="DEMO-1",
        summary="Add hello() to greeter.py",
        description="Implement hello() returning 'hello'.",
        status="To Do",
        issue_type="Story",
        acceptance_criteria="- Unit tests pass\n- hello() returns 'hello'",
    )
    assert assess_ticket_complexity(ticket) != PromptComplexity.COMPLEX


def test_assess_ticket_complexity_rejects_api_epic() -> None:
    ticket = JiraTicket(
        key="DEMO-2",
        summary="Build REST API for tasks",
        description="CRUD endpoints with SQLite and auth middleware.",
        status="To Do",
        issue_type="Epic",
        acceptance_criteria="All endpoints covered by integration tests",
    )
    assert assess_ticket_complexity(ticket) == PromptComplexity.COMPLEX


def _story(key: str) -> BacklogStory:
    return BacklogStory(key=key, summary=f"Story {key}")


def test_normalize_backlog_plan_merges_trivial_over_split() -> None:
    plan = BacklogPlan(
        product_brief=ProductBrief(title="Greeter hello", summary="Add hello"),
        stories=[_story("A"), _story("B"), _story("C")],
        recommended_first="A",
    )
    prompt = "Add hello() to greeter.py with pytest."
    normalized = normalize_backlog_plan(plan, user_prompt=prompt)
    assert len(normalized.stories) == 1
    assert normalized.recommended_first == "A"


def test_normalize_backlog_plan_caps_story_count(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MAX_BACKLOG_STORIES", "2")
    get_settings.cache_clear()

    plan = BacklogPlan(
        product_brief=ProductBrief(title="Big", summary="Many slices"),
        stories=[_story("A"), _story("B"), _story("C")],
        recommended_first="C",
    )
    prompt = "Improve logging across three modules with separate PRs for each."
    normalized = normalize_backlog_plan(plan, user_prompt=prompt)
    assert len(normalized.stories) == 2
    assert [story.key for story in sort_stories(normalized)] == ["A", "B"]
    assert normalized.recommended_first == "A"

    get_settings.cache_clear()


def test_normalize_backlog_plan_caps_vector_e2e_prompt_to_three() -> None:
    from tests.helpers.from_prompt_live import VECTOR_TRAP_PROMPT

    plan = BacklogPlan(
        product_brief=ProductBrief(title="Notify", summary="Outbound subsystem"),
        stories=[_story("A"), _story("B"), _story("C"), _story("D")],
        recommended_first="A",
    )
    normalized = normalize_backlog_plan(plan, user_prompt=VECTOR_TRAP_PROMPT)
    assert len(normalized.stories) == 3
    assert [story.key for story in sort_stories(normalized)] == ["A", "B", "C"]


def test_normalize_backlog_plan_drops_greeter_only_story() -> None:
    plan = BacklogPlan(
        product_brief=ProductBrief(title="Notify", summary="Outbound subsystem"),
        stories=[
            _story("A"),
            BacklogStory(
                key="B",
                summary="Ensure greeter tests unaffected",
                description="No changes to greeter",
                acceptance_criteria="greeter tests still pass",
            ),
            _story("C"),
        ],
        recommended_first="A",
    )
    prompt = "Split into 2-3 stories for notification subsystem."
    normalized = normalize_backlog_plan(plan, user_prompt=prompt)
    assert len(normalized.stories) == 2
    assert {story.key for story in normalized.stories} == {"A", "C"}


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


def _init_repo(tmp_path) -> None:
    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "test"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )
    readme = tmp_path / "README.md"
    readme.write_text("parent\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=tmp_path, check=True, capture_output=True)


def test_prepare_chained_workspace_copies_parent_and_creates_branch(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from sprint_crew.config import get_settings

    monkeypatch.setenv("SPRINT_WORKSPACE_BASE", str(tmp_path / "workspaces"))
    get_settings.cache_clear()

    parent = tmp_path / "parent"
    parent.mkdir()
    _init_repo(parent)
    (parent / "src").mkdir()
    (parent / "src" / "feature.py").write_text("done = True\n", encoding="utf-8")

    child = prepare_chained_workspace(parent, "child-session-12345678")
    assert child.exists()
    assert (child / "src" / "feature.py").read_text(encoding="utf-8") == "done = True\n"

    branch = subprocess.run(
        ["git", "branch", "--show-current"],
        cwd=child,
        capture_output=True,
        text=True,
        check=True,
    )
    assert branch.stdout.strip() == "feature/child-se"
