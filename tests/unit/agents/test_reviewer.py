from __future__ import annotations

from unittest.mock import patch

import pytest

from sprint_crew.agents.reviewer import _run_acceptance_tests, run_reviewer
from sprint_crew.schemas.change import CodeChange, ReviewOutcome
from sprint_crew.schemas.ticket import PlanStep, TaskPlan


@pytest.fixture
def greeter_plan() -> TaskPlan:
    return TaskPlan(
        ticket_key="DEMO-1",
        summary="Add hello() to greeter.py",
        steps=[PlanStep(description="implement hello", files=["greeter.py"])],
        files_to_touch=["greeter.py"],
        acceptance_tests=["pytest -q tests/test_greeter.py"],
    )


@pytest.fixture
def greeter_code_change() -> CodeChange:
    return CodeChange(
        ticket_key="DEMO-1",
        branch="feature/demo-1",
        summary="added hello",
        tests_passed=True,
    )


def test_run_acceptance_tests_green_on_fixture(tmp_workspace, greeter_plan: TaskPlan) -> None:
    (tmp_workspace / "greeter.py").write_text(
        'def hello():\n    return "hello"\n',
        encoding="utf-8",
    )
    output, passed = _run_acceptance_tests(tmp_workspace, greeter_plan.acceptance_tests)
    assert passed is True
    assert "exit_code=0" in output


def test_run_acceptance_tests_red_when_tests_fail(tmp_workspace, greeter_plan: TaskPlan) -> None:
    (tmp_workspace / "greeter.py").write_text("# empty\n", encoding="utf-8")
    output, passed = _run_acceptance_tests(tmp_workspace, greeter_plan.acceptance_tests)
    assert passed is False
    assert "exit_code=" in output


@pytest.mark.asyncio
async def test_run_reviewer_orchestrator_overrides_llm_tests_passed(
    tmp_workspace,
    greeter_plan: TaskPlan,
    greeter_code_change: CodeChange,
) -> None:
    (tmp_workspace / "greeter.py").write_text(
        'def hello():\n    return "hello"\n',
        encoding="utf-8",
    )
    llm_review = ReviewOutcome(
        ticket_key="DEMO-1",
        passed=True,
        summary="ok",
        tests_passed=False,
    )
    with patch(
        "sprint_crew.agents.reviewer.structured_completion",
        return_value=llm_review,
    ) as completion_mock:
        result = await run_reviewer(
            greeter_plan,
            greeter_code_change,
            tmp_workspace,
            ticket_acceptance_criteria="- hello() returns 'hello'",
        )

    assert result.tests_passed is True
    assert completion_mock.call_count == 1


@pytest.mark.asyncio
async def test_run_reviewer_fails_when_acceptance_red(
    tmp_workspace,
    greeter_plan: TaskPlan,
    greeter_code_change: CodeChange,
) -> None:
    (tmp_workspace / "greeter.py").write_text("# broken\n", encoding="utf-8")
    llm_review = ReviewOutcome(
        ticket_key="DEMO-1",
        passed=True,
        summary="looks fine",
        tests_passed=True,
    )
    with patch(
        "sprint_crew.agents.reviewer.structured_completion",
        return_value=llm_review,
    ):
        result = await run_reviewer(greeter_plan, greeter_code_change, tmp_workspace)

    assert result.tests_passed is False
    assert result.passed is False


@pytest.mark.asyncio
async def test_run_reviewer_overrides_llm_passed_false_when_green(
    tmp_workspace,
    greeter_plan: TaskPlan,
    greeter_code_change: CodeChange,
) -> None:
    (tmp_workspace / "greeter.py").write_text(
        'def hello():\n    return "hello"\n',
        encoding="utf-8",
    )
    llm_review = ReviewOutcome(
        ticket_key="DEMO-1",
        passed=False,
        summary="ok",
        tests_passed=True,
        findings=[],
    )
    with patch(
        "sprint_crew.agents.reviewer.structured_completion",
        return_value=llm_review,
    ):
        result = await run_reviewer(greeter_plan, greeter_code_change, tmp_workspace)

    assert result.tests_passed is True
    assert result.passed is True


@pytest.mark.asyncio
async def test_run_reviewer_passes_workspace_diff_to_prompt(
    tmp_workspace,
    greeter_plan: TaskPlan,
    greeter_code_change: CodeChange,
) -> None:
    (tmp_workspace / "greeter.py").write_text(
        'def hello():\n    return "hello"\n',
        encoding="utf-8",
    )
    diff_snippet = "diff --git a/greeter.py b/greeter.py\n+def hello"
    llm_review = ReviewOutcome(
        ticket_key="DEMO-1",
        passed=True,
        summary="ok",
        tests_passed=True,
    )
    with patch(
        "sprint_crew.agents.reviewer.structured_completion",
        return_value=llm_review,
    ) as completion_mock:
        await run_reviewer(
            greeter_plan,
            greeter_code_change,
            tmp_workspace,
            workspace_diff=diff_snippet,
            tests_already_run=True,
        )

    user_prompt = completion_mock.call_args.kwargs["user_prompt"]
    assert diff_snippet in user_prompt
