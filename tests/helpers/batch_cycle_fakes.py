from __future__ import annotations

from pathlib import Path

from sprint_crew.schemas.backlog import BacklogPlan, BacklogStory, ProductBrief
from sprint_crew.schemas.session import SessionStatus, SprintSession
from sprint_crew.schemas.ticket import JiraTicket


def trivial_ticket(key: str) -> JiraTicket:
    return JiraTicket(
        key=key,
        summary=f"Add hello() to greeter module ({key})",
        description="Implement hello() returning 'hello'.",
        status="To Do",
        issue_type="Story",
        acceptance_criteria="- Unit tests pass\n- hello() returns 'hello'",
    )


def trivial_plan(*keys: str) -> BacklogPlan:
    stories = [
        BacklogStory(key=key, summary=f"Story {key}", description="Add hello() to greeter.")
        for key in keys
    ]
    return BacklogPlan(
        product_brief=ProductBrief(title="Greeter", summary="Hello work"),
        stories=stories,
        recommended_first=keys[0],
    )


def awaiting_session(session_id: str, ticket: JiraTicket, workspace: Path) -> SprintSession:
    return SprintSession(
        session_id=session_id,
        status=SessionStatus.AWAITING_HUMAN,
        ticket_key=ticket.key,
        workspace_root=str(workspace),
        selected_ticket=ticket,
    )
