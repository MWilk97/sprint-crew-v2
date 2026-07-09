from __future__ import annotations

from sprint_crew.schemas.session import SprintSession


def planning_mode_from_session(session: SprintSession) -> str | None:
    for event in session.events:
        if event.agent == "tech_lead" and event.event_type == "plan_created":
            detail = event.detail or {}
            mode = detail.get("mode")
            if isinstance(mode, str):
                return mode
    return None
