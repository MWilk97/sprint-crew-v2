from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from sprint_crew.agents.tech_lead_planning import run_tech_lead_validated
from sprint_crew.orchestrator.plan_validation import (
    PlanScopeValidationError,
    PlanStructureValidationError,
    validate_plan_scope_conflicts,
    validate_plan_structure,
)
from sprint_crew.schemas.ticket import JiraTicket, PlanStep, TaskPlan


def test_validate_plan_structure_rejects_unused_files_to_touch() -> None:
    ticket = JiraTicket(
        key="DEMO-1",
        summary="REST API queue integration with SQLite",
        status="To Do",
        issue_type="Story",
    )
    plan = TaskPlan(
        ticket_key="DEMO-1",
        summary="queue",
        steps=[PlanStep(description="edit repo", files=["src/storage/sqlite_repo.py"])],
        files_to_touch=["src/storage/sqlite_repo.py", "src/messaging/queue_worker.py"],
        acceptance_tests=["pytest -q"],
    )
    with pytest.raises(PlanStructureValidationError, match="queue_worker"):
        validate_plan_structure(plan, ticket)


def test_validate_plan_structure_rejects_baseline_test_in_steps(tmp_path: Path) -> None:
    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()
    (tests_dir / "test_ferry_retry.py").write_text("pass\n", encoding="utf-8")
    ticket = JiraTicket(
        key="SCRUM-2",
        summary="retry policy",
        status="To Do",
        issue_type="Story",
    )
    plan = TaskPlan(
        ticket_key="SCRUM-2",
        summary="retry",
        steps=[
            PlanStep(
                description="implement retry",
                files=["src/messaging/retry_policy.py"],
            ),
            PlanStep(
                description="verify tests",
                files=["tests/test_ferry_retry.py"],
            ),
        ],
        files_to_touch=["src/messaging/retry_policy.py", "tests/test_ferry_retry.py"],
        acceptance_tests=["pytest tests/test_ferry_retry.py -q"],
    )
    baseline = frozenset({"tests/test_ferry_retry.py"})
    with pytest.raises(PlanStructureValidationError, match="existing test"):
        validate_plan_structure(
            plan,
            ticket,
            workspace_root=tmp_path,
            baseline_paths=baseline,
        )


def test_validate_plan_structure_allows_tests_only_in_files_to_touch() -> None:
    ticket = JiraTicket(
        key="DEMO-1",
        summary="REST API queue integration",
        status="To Do",
        issue_type="Story",
    )
    plan = TaskPlan(
        ticket_key="DEMO-1",
        summary="queue",
        steps=[PlanStep(description="edit repo", files=["src/storage/sqlite_repo.py"])],
        files_to_touch=["src/storage/sqlite_repo.py", "tests/test_ferry_queue.py"],
        acceptance_tests=["pytest -q tests/test_ferry_queue.py"],
    )
    validate_plan_structure(plan, ticket)


def test_validate_plan_structure_allows_new_test_in_steps(tmp_path: Path) -> None:
    ticket = JiraTicket(
        key="DEMO-1",
        summary="add worker tests",
        status="To Do",
        issue_type="Story",
    )
    plan = TaskPlan(
        ticket_key="DEMO-1",
        summary="worker",
        steps=[
            PlanStep(
                description="create new test file",
                files=["src/worker.py", "tests/test_worker.py"],
            )
        ],
        files_to_touch=["src/worker.py", "tests/test_worker.py"],
        acceptance_tests=["pytest tests/test_worker.py -q"],
    )
    validate_plan_structure(
        plan,
        ticket,
        workspace_root=tmp_path,
        baseline_paths=frozenset(),
    )


def test_validate_plan_scope_conflicts_rejects_overlap() -> None:
    plan = TaskPlan(
        ticket_key="DEMO-1",
        summary="queue",
        steps=[
            PlanStep(
                description="wire queue worker",
                files=["src/messaging/queue_worker.py", "src/storage/sqlite_repo.py"],
            )
        ],
        files_to_touch=["src/messaging/queue_worker.py", "src/storage/sqlite_repo.py"],
        out_of_scope=["src/storage/sqlite_repo.py"],
        acceptance_tests=["pytest -q tests/test_ferry_queue.py"],
    )
    with pytest.raises(PlanScopeValidationError, match="sqlite_repo"):
        validate_plan_scope_conflicts(plan)


def test_validate_plan_scope_conflicts_allows_disjoint_scope() -> None:
    plan = TaskPlan(
        ticket_key="DEMO-1",
        summary="queue",
        steps=[PlanStep(description="edit worker", files=["src/messaging/queue_worker.py"])],
        files_to_touch=["src/messaging/queue_worker.py"],
        out_of_scope=["src/messaging/retry_policy.py"],
        acceptance_tests=["pytest -q"],
    )
    validate_plan_scope_conflicts(plan)


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
async def test_run_tech_lead_validated_template_fallback_after_two_failures(tmp_path) -> None:
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

    with patch("sprint_crew.agents.tech_lead_planning.run_tech_lead", new=run_mock):
        plan, mode, _tool_log = await run_tech_lead_validated(ticket, tmp_path)

    assert mode == "template_fallback"
    assert plan.ticket_key == "DEMO-1"
    assert run_mock.await_count == 3
