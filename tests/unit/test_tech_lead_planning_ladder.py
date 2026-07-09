from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from sprint_crew.agents import tech_lead as tech_lead_module
from sprint_crew.agents.tech_lead_planning import run_tech_lead_validated
from sprint_crew.schemas.ticket import JiraTicket, PlanStep, TaskPlan


@pytest.mark.asyncio
async def test_run_tech_lead_validated_returns_template_mode_for_greeter(tmp_path) -> None:
    ticket = JiraTicket(
        key="DEMO-1",
        summary="Add hello() to greeter.py",
        description="Implement hello() returning 'hello'.",
        status="To Do",
        issue_type="Story",
        acceptance_criteria="- Unit tests pass",
    )
    plan, mode, _tool_log = await run_tech_lead_validated(ticket, tmp_path)
    assert mode == "template"
    assert plan.ticket_key == "DEMO-1"


@pytest.mark.asyncio
async def test_run_tech_lead_complex_uses_tool_loop(tmp_path) -> None:
    ticket = JiraTicket(
        key="DEMO-2",
        summary="Build REST API for tasks",
        description="CRUD endpoints with SQLite storage.",
        status="To Do",
        issue_type="Story",
        acceptance_criteria="Integration tests pass",
    )
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
