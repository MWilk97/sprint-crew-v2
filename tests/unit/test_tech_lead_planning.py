from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from tests.helpers.agent_live_tickets import complex_api_ticket, greeter_ticket

from sprint_crew.agents import tech_lead as tech_lead_module
from sprint_crew.agents.tech_lead_planning import run_tech_lead_validated
from sprint_crew.config import get_settings
from sprint_crew.schemas.ticket import JiraTicket, PlanStep, TaskPlan


@pytest.mark.asyncio
async def test_run_tech_lead_validated_retries_once_on_invalid_commands(tmp_path) -> None:
    (tmp_path / "greeter.py").write_text("pass\n", encoding="utf-8")
    ticket = JiraTicket(
        key="DEMO-1",
        summary="Add hello()",
        status="To Do",
        issue_type="Story",
    )
    bad_plan = TaskPlan(
        ticket_key="DEMO-1",
        summary="Add hello()",
        steps=[PlanStep(description="edit", files=["greeter.py"])],
        acceptance_tests=["pytest -q passes"],
    )
    good_plan = bad_plan.model_copy(update={"acceptance_tests": ["pytest -q"]})
    run_mock = AsyncMock(
        side_effect=[(bad_plan, "static"), (good_plan, "static")],
    )

    with patch("sprint_crew.agents.tech_lead_planning.run_tech_lead", new=run_mock):
        plan, mode, _tool_log = await run_tech_lead_validated(ticket, tmp_path)

    assert plan.acceptance_tests == ["pytest -q"]
    assert mode == "static"
    assert run_mock.await_count == 2
    second_call_kwargs = run_mock.await_args_list[1].kwargs
    assert "validation failed" in second_call_kwargs["prior_review_feedback"].lower()


@pytest.mark.asyncio
async def test_run_tech_lead_validated_template_fallback_after_two_failures(
    tmp_path, monkeypatch
) -> None:
    ticket = JiraTicket(
        key="DEMO-1",
        summary="Add hello() to greeter.py",
        description="Implement hello() returning 'hello'.",
        status="To Do",
        issue_type="Story",
        acceptance_criteria="- Unit tests pass\n- hello() returns 'hello'",
    )
    bad_plan = TaskPlan(
        ticket_key="DEMO-1",
        summary="Add hello()",
        steps=[PlanStep(description="edit", files=["greeter.py"])],
        acceptance_tests=["pytest -q passes"],
    )
    run_mock = AsyncMock(return_value=(bad_plan, "static"))

    # Pin plan retries so the attempt count (max(3, MAX_PLAN_RETRIES + 2)) is
    # independent of the production default.
    monkeypatch.setenv("MAX_PLAN_RETRIES", "1")
    get_settings.cache_clear()
    try:
        with patch("sprint_crew.agents.tech_lead_planning.run_tech_lead", new=run_mock):
            plan, mode, _tool_log = await run_tech_lead_validated(ticket, tmp_path)
    finally:
        get_settings.cache_clear()

    assert mode == "template_fallback"
    assert plan.ticket_key == "DEMO-1"
    assert run_mock.await_count == 3


@pytest.mark.asyncio
async def test_run_tech_lead_validated_returns_template_mode_for_greeter(tmp_path) -> None:
    ticket = greeter_ticket(summary="Add hello() to greeter.py")
    plan, mode, _tool_log = await run_tech_lead_validated(ticket, tmp_path)
    assert mode == "template"
    assert plan.ticket_key == "DEMO-1"


@pytest.mark.asyncio
async def test_run_tech_lead_complex_uses_tool_loop(tmp_path) -> None:
    ticket = complex_api_ticket()
    expected = TaskPlan(
        ticket_key="DEMO-2",
        summary="api",
        steps=[PlanStep(description="routes", files=["routes.py"])],
        files_to_touch=["routes.py"],
        acceptance_tests=["pytest -q"],
    )
    with (
        patch.object(
            tech_lead_module,
            "run_tech_lead_loop",
            new=AsyncMock(return_value="handoff text"),
        ) as loop_mock,
        patch.object(
            tech_lead_module,
            "_repo_context_for_ticket",
            return_value="REAL_REPO_CONTEXT",
        ),
        patch.object(
            tech_lead_module,
            "_structured_plan_from_context",
            return_value=expected,
        ) as structured_mock,
        patch(
            "sprint_crew.orchestrator.template_plan.build_template_task_plan_validated",
            side_effect=RuntimeError("skip template for complex"),
        ),
    ):
        plan, mode = await tech_lead_module.run_tech_lead(ticket, tmp_path)

    loop_mock.assert_awaited_once()
    structured_mock.assert_called_once()
    call_kwargs = structured_mock.call_args.kwargs
    assert call_kwargs["repo_context"] == "REAL_REPO_CONTEXT"
    assert call_kwargs["planning_handoff"] == "handoff text"
    assert mode == "tool_loop"
    assert plan.ticket_key == "DEMO-2"
