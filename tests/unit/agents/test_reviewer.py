from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from tests.helpers.ticket_fixtures import greeter_code_change, greeter_task_plan

from sprint_crew.agents.reviewer import run_reviewer
from sprint_crew.schemas.change import ReviewOutcome

_GREEN_RUN = ("exit_code=0", True)
_RED_RUN = ("exit_code=1\nFAILED tests/test_greeter.py::test_hello", False)


@pytest.mark.asyncio
async def test_run_reviewer_orchestrator_overrides_llm_tests_passed(tmp_workspace) -> None:
    plan = greeter_task_plan()
    change = greeter_code_change()
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
    with (
        patch("sprint_crew.agents.reviewer.run_acceptance_tests", return_value=_GREEN_RUN),
        patch(
            "sprint_crew.agents.reviewer.structured_completion",
            return_value=llm_review,
        ) as completion_mock,
    ):
        result = await run_reviewer(
            plan,
            change,
            tmp_workspace,
            ticket_acceptance_criteria="- hello() returns 'hello'",
        )

    assert result.tests_passed is True
    assert completion_mock.call_count == 1


@pytest.mark.asyncio
async def test_run_reviewer_fails_when_acceptance_red(tmp_workspace) -> None:
    plan = greeter_task_plan()
    change = greeter_code_change()
    (tmp_workspace / "greeter.py").write_text("# broken\n", encoding="utf-8")
    llm_review = ReviewOutcome(
        ticket_key="DEMO-1",
        passed=True,
        summary="looks fine",
        tests_passed=True,
    )
    with (
        patch("sprint_crew.agents.reviewer.run_acceptance_tests", return_value=_RED_RUN),
        patch(
            "sprint_crew.agents.reviewer.structured_completion",
            return_value=llm_review,
        ),
    ):
        result = await run_reviewer(plan, change, tmp_workspace)

    assert result.tests_passed is False
    assert result.passed is False


@pytest.mark.asyncio
async def test_run_reviewer_overrides_llm_passed_false_when_green(tmp_workspace) -> None:
    plan = greeter_task_plan()
    change = greeter_code_change()
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
    with (
        patch("sprint_crew.agents.reviewer.run_acceptance_tests", return_value=_GREEN_RUN),
        patch(
            "sprint_crew.agents.reviewer.structured_completion",
            return_value=llm_review,
        ),
    ):
        result = await run_reviewer(plan, change, tmp_workspace)

    assert result.tests_passed is True
    assert result.passed is True


@pytest.mark.asyncio
async def test_run_reviewer_passes_workspace_diff_to_prompt(tmp_workspace) -> None:
    plan = greeter_task_plan()
    change = greeter_code_change()
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
            plan,
            change,
            tmp_workspace,
            workspace_diff=diff_snippet,
            prior_test_output="$ pytest -q\n1 passed\n",
            prior_tests_verified=True,
        )

    user_prompt = completion_mock.call_args.kwargs["user_prompt"]
    assert diff_snippet in user_prompt


@pytest.mark.asyncio
async def test_run_reviewer_no_max_tokens_and_retries(tmp_workspace) -> None:
    plan = greeter_task_plan()
    change = greeter_code_change()
    (tmp_workspace / "greeter.py").write_text(
        'def hello():\n    return "hello"\n',
        encoding="utf-8",
    )
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
            plan,
            change,
            tmp_workspace,
            prior_test_output="$ pytest -q\n1 passed\n",
            prior_tests_verified=True,
        )

    kwargs = completion_mock.call_args.kwargs
    assert kwargs["max_retries"] == 3
    assert kwargs["timeout_seconds"] == 600
    assert kwargs.get("max_tokens") is None


@pytest.mark.asyncio
async def test_reviewer_is_shown_the_real_test_output_not_a_claim(tmp_workspace) -> None:
    """The prompt must carry the acceptance run's output, never a sentence about it.

    Telling the Reviewer to "trust exit_code=0" is how a Coder's unverified self-report
    became a Reviewer's confident "tests passed" — the model echoed an invented fact.
    """
    real_output = "$ pytest -q tests/test_greeter.py\n1 passed in 0.01s\nexit_code=0"
    llm_review = ReviewOutcome(ticket_key="DEMO-1", passed=True, summary="ok", tests_passed=True)
    with patch(
        "sprint_crew.agents.reviewer.structured_completion", return_value=llm_review
    ) as completion_mock:
        await run_reviewer(
            greeter_task_plan(),
            greeter_code_change(),
            tmp_workspace,
            prior_test_output=real_output,
            prior_tests_verified=True,
        )

    user_prompt = completion_mock.call_args.kwargs["user_prompt"]
    assert real_output in user_prompt
    assert "Trust exit_code=0" not in user_prompt


@pytest.mark.asyncio
async def test_reviewer_reruns_tests_when_the_prior_run_was_not_verified(tmp_workspace) -> None:
    llm_review = ReviewOutcome(ticket_key="DEMO-1", passed=True, summary="ok", tests_passed=True)
    with (
        patch(
            "sprint_crew.agents.reviewer.run_acceptance_tests",
            new=AsyncMock(return_value=("$ pytest -q\ncollected 0 items / 1 error", False)),
        ) as run_mock,
        patch("sprint_crew.agents.reviewer.structured_completion", return_value=llm_review),
    ):
        outcome = await run_reviewer(
            greeter_task_plan(),
            greeter_code_change(),
            tmp_workspace,
            prior_test_output="claimed green, never run",
            prior_tests_verified=False,
        )

    run_mock.assert_awaited_once()
    # The harness's own run overrides whatever the model said about the tests.
    assert outcome.tests_passed is False
