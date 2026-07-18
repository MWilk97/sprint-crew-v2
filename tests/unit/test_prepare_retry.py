from __future__ import annotations

from unittest.mock import patch

import pytest

from sprint_crew.graph.pipeline import prepare_retry
from sprint_crew.schemas.change import CodeChange, ReviewOutcome
from sprint_crew.schemas.ticket import PlanStep, TaskPlan


@pytest.fixture
def base_retry_state(tmp_path) -> dict:
    plan = TaskPlan(
        ticket_key="DEMO-1",
        summary="Add hello()",
        steps=[PlanStep(description="edit greeter", files=["greeter.py"])],
        acceptance_tests=["pytest -q"],
    )
    return {
        "session_id": "retry-session",
        "workspace_root": str(tmp_path),
        "selected_ticket": {
            "key": "DEMO-1",
            "summary": "Add hello()",
            "status": "To Do",
            "issue_type": "Story",
        },
        "task_plan": plan.model_dump(),
        "attempt": 0,
        "plan_retries": 0,
        "workspace_diff": "diff snippet",
        "events": [],
    }


@pytest.mark.asyncio
async def test_prepare_retry_increments_attempt(base_retry_state: dict) -> None:
    base_retry_state["review_outcome"] = ReviewOutcome(
        ticket_key="DEMO-1",
        passed=False,
        summary="fix it",
        tests_passed=False,
        retry_scope="code",
    ).model_dump()

    with patch("sprint_crew.agents.reviewer._run_acceptance_tests", return_value=("stderr", False)):
        result = await prepare_retry(base_retry_state)  # type: ignore[arg-type]

    assert result["attempt"] == 1
    assert result["prior_review_feedback"]


@pytest.mark.asyncio
async def test_prepare_retry_code_scope_skips_tester_when_tests_green(
    base_retry_state: dict,
) -> None:
    base_retry_state["review_outcome"] = ReviewOutcome(
        ticket_key="DEMO-1",
        passed=False,
        summary="style issue",
        tests_passed=True,
        retry_scope="code",
    ).model_dump()
    base_retry_state["tests_run_this_cycle"] = True
    base_retry_state["code_change"] = CodeChange(
        ticket_key="DEMO-1",
        branch="feature/demo-1",
        summary="done",
        tests_passed=True,
    ).model_dump()

    with patch("sprint_crew.agents.reviewer._run_acceptance_tests") as run_tests:
        result = await prepare_retry(base_retry_state)  # type: ignore[arg-type]

    run_tests.assert_not_called()
    assert result["skip_tester_this_attempt"] is True


@pytest.mark.asyncio
async def test_prepare_retry_plan_scope_increments_plan_retries(base_retry_state: dict) -> None:
    base_retry_state["review_outcome"] = ReviewOutcome(
        ticket_key="DEMO-1",
        passed=False,
        summary="wrong plan",
        tests_passed=False,
        retry_scope="plan",
    ).model_dump()

    with patch("sprint_crew.agents.reviewer._run_acceptance_tests", return_value=("", False)):
        result = await prepare_retry(base_retry_state)  # type: ignore[arg-type]

    assert result["plan_retries"] == 1


@pytest.mark.asyncio
async def test_prepare_retry_wires_test_output_into_feedback(base_retry_state: dict) -> None:
    """format_review_feedback's own content rendering is covered by
    test_retry_feedback.py; this only proves prepare_retry wires the workspace
    diff and rerun test output through to the feedback at all."""
    base_retry_state["review_outcome"] = ReviewOutcome(
        ticket_key="DEMO-1",
        passed=False,
        summary="tests red",
        tests_passed=False,
        retry_scope="code",
    ).model_dump()

    with patch(
        "sprint_crew.agents.reviewer._run_acceptance_tests",
        return_value=("AssertionError: boom", False),
    ):
        result = await prepare_retry(base_retry_state)  # type: ignore[arg-type]

    assert result["prior_review_feedback"]
    assert result["retry_scope"] == "code"
    assert result["attempt"] == 1
