from __future__ import annotations

from sprint_crew.orchestrator.template_plan import (
    build_template_task_plan,
    build_template_task_plan_validated,
)
from sprint_crew.schemas.ticket import JiraTicket


def test_build_template_task_plan_greeter() -> None:
    ticket = JiraTicket(
        key="DEMO-1",
        summary="Add hello() to greeter.py",
        description="Implement hello() returning 'hello'.",
        status="To Do",
        issue_type="Story",
        acceptance_criteria="- Unit tests pass\n- hello() returns 'hello'",
    )
    plan = build_template_task_plan_validated(ticket)
    assert plan.ticket_key == "DEMO-1"
    assert plan.acceptance_tests[0].startswith("pytest")
    assert "greeter.py" in plan.files_to_touch
    assert len(plan.steps) == 1


def test_build_template_task_plan_greeter_module_excludes_tests_from_files_to_touch() -> None:
    ticket = JiraTicket(
        key="DEMO-1",
        summary="Add hello() to greeter module",
        description="Implement hello() returning 'hello'.",
        status="To Do",
        issue_type="Story",
        acceptance_criteria="pytest -q tests/test_greeter.py passes",
    )
    plan = build_template_task_plan_validated(ticket)
    assert plan.files_to_touch == ["greeter.py"]
    assert "tests/test_greeter.py" not in plan.files_to_touch
    assert plan.acceptance_tests[0].startswith("pytest tests/test_greeter.py")


def test_build_template_task_plan_infers_source_from_test_path() -> None:
    ticket = JiraTicket(
        key="SCRUM-1",
        summary="[sprint-crew-test] greeter ship_live greeter",
        description="Implement hello() returning 'hello' and ensure pytest passes.",
        status="To Do",
        issue_type="Story",
        acceptance_criteria="pytest -q tests/test_greeter.py passes",
    )
    plan = build_template_task_plan_validated(ticket)
    assert plan.files_to_touch == ["greeter.py"]
    assert plan.acceptance_tests[0].startswith("pytest tests/test_greeter.py")


def test_build_template_task_plan_targeted_pytest() -> None:
    ticket = JiraTicket(
        key="DEMO-2",
        summary="Fix greeter test",
        description="Run pytest tests/test_greeter.py -q",
        status="To Do",
        issue_type="Story",
        acceptance_criteria="pytest tests/test_greeter.py -q",
    )
    plan = build_template_task_plan(ticket)
    assert plan.acceptance_tests[0].startswith("pytest tests/test_greeter.py")
