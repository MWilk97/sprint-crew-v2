from __future__ import annotations

from contextlib import contextmanager
from unittest.mock import patch

from sprint_crew.schemas.change import CodeChange
from sprint_crew.schemas.ticket import JiraTicket, PlanStep, TaskPlan


def greeter_ticket(**overrides: object) -> JiraTicket:
    defaults = {
        "key": "DEMO-1",
        "summary": "Add hello() to greeter module",
        "description": "Implement hello() returning 'hello'.",
        "status": "To Do",
        "issue_type": "Story",
        "acceptance_criteria": "- Unit tests pass\n- hello() returns 'hello'",
    }
    defaults.update(overrides)
    return JiraTicket(**defaults)


def greeter_task_plan(**overrides: object) -> TaskPlan:
    defaults = {
        "ticket_key": "DEMO-1",
        "summary": "Add hello() to greeter.py",
        "steps": [PlanStep(description="implement hello", files=["greeter.py"])],
        "files_to_touch": ["greeter.py"],
        "acceptance_tests": ["pytest -q tests/test_greeter.py"],
    }
    defaults.update(overrides)
    return TaskPlan(**defaults)


def greeter_code_change(**overrides: object) -> CodeChange:
    defaults = {
        "ticket_key": "DEMO-1",
        "branch": "feature/demo-1",
        "summary": "added hello",
        "tests_passed": True,
    }
    defaults.update(overrides)
    return CodeChange(**defaults)


def email_validators_ticket() -> JiraTicket:
    return JiraTicket(
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


def complex_api_ticket() -> JiraTicket:
    return JiraTicket(
        key="DEMO-3",
        summary="Build REST API for tasks",
        description=(
            "Create routes.py with CRUD endpoints for tasks backed by SQLite storage. "
            "Include models.py for the schema and tests under tests/."
        ),
        status="To Do",
        issue_type="Story",
        acceptance_criteria="pytest -q tests/test_routes.py",
    )


def vector_story1_ticket() -> JiraTicket:
    return JiraTicket(
        key="SCRUM-1",
        summary="Persistent outbound message queue using ferry dispatch layer",
        description=(
            "Implement SQLite-backed outbound queue in sqlite_repo.py and wire QueueWorker "
            "to dequeue pending messages through MessageFerry. Follow existing messaging patterns."
        ),
        status="To Do",
        issue_type="Story",
        acceptance_criteria="pytest -q tests/test_ferry_queue.py passes",
    )


def vector_story2_ticket() -> JiraTicket:
    return JiraTicket(
        key="SCRUM-2",
        summary="Exponential backoff retry when adapter handoff fails",
        description=(
            "Implement exponential backoff in retry_policy.py: 3 attempts with 100ms base delay "
            "when MessageFerry adapter handoff fails. Follow existing messaging patterns."
        ),
        status="To Do",
        issue_type="Story",
        acceptance_criteria="pytest -q tests/test_ferry_retry.py passes",
    )


def vector_story3_ticket() -> JiraTicket:
    return JiraTicket(
        key="SCRUM-3",
        summary="REST endpoints to enqueue notifications and query delivery status",
        description=(
            "Add notification REST endpoints to src/api/routes.py: POST /notifications to enqueue "
            "and GET /notifications/{id} to query delivery status. Integrate with the outbound "
            "queue and ferry dispatch layer from prior stories."
        ),
        status="To Do",
        issue_type="Story",
        acceptance_criteria="pytest -q tests/test_notify_routes.py passes",
    )


@contextmanager
def skip_template_fast_path():
    """Force TechLead static/tool_loop paths — template unavailable for this ticket."""
    with patch(
        "sprint_crew.orchestrator.template_plan.build_template_task_plan_validated",
        side_effect=RuntimeError("template unavailable for live work lane test"),
    ):
        yield
