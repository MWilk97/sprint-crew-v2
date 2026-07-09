from __future__ import annotations

from unittest.mock import patch

import pytest

from sprint_crew.agents.formatter import run_formatter
from sprint_crew.schemas.change import CodeChange
from sprint_crew.schemas.ticket import TaskPlan


@pytest.mark.asyncio
async def test_formatter_always_sets_canonical_branch(task_plan: TaskPlan) -> None:
    llm_change = CodeChange(
        ticket_key="WRONG",
        branch="SCRUM-99-custom-branch",
        summary="done",
        tests_passed=True,
    )
    with patch(
        "sprint_crew.agents.formatter.structured_completion",
        return_value=llm_change,
    ):
        result = await run_formatter(
            task_plan=task_plan,
            raw_output="handoff",
            git_diff="diff",
        )
    assert result.ticket_key == task_plan.ticket_key
    assert result.branch == f"feature/{task_plan.ticket_key.lower()}"


@pytest.mark.asyncio
async def test_formatter_overrides_llm_ticket_key_and_branch(task_plan: TaskPlan) -> None:
    llm_change = CodeChange(
        ticket_key="WRONG",
        branch="SCRUM-99-custom-branch",
        summary="done",
        tests_passed=False,
    )
    with patch(
        "sprint_crew.agents.formatter.structured_completion",
        return_value=llm_change,
    ):
        result = await run_formatter(
            task_plan=task_plan,
            raw_output="handoff",
            git_diff="diff",
        )
    assert result.ticket_key == task_plan.ticket_key
    assert result.branch == f"feature/{task_plan.ticket_key.lower()}"
    assert result.tests_passed is False


@pytest.mark.asyncio
async def test_formatter_passes_git_diff_into_prompt(task_plan: TaskPlan) -> None:
    llm_change = CodeChange(
        ticket_key="DEMO-1",
        branch="feature/demo-1",
        summary="done",
        tests_passed=True,
    )
    diff = "diff --git a/greeter.py b/greeter.py\n+def hello():"
    with patch(
        "sprint_crew.agents.formatter.structured_completion",
        return_value=llm_change,
    ) as completion_mock:
        await run_formatter(
            task_plan=task_plan,
            raw_output="handoff",
            git_diff=diff,
        )

    user_prompt = completion_mock.call_args.kwargs["user_prompt"]
    assert diff in user_prompt
