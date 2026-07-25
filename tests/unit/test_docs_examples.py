from __future__ import annotations

import json

from sprint_crew.config import get_settings
from sprint_crew.schemas.session import SprintSession


def test_session_timeline_example_validates_as_sprint_session() -> None:
    """The FE reads docs/examples/session-timeline.json as a reference; keep it a valid
    SprintSession so it cannot silently rot (it previously omitted `summary`/`workspace_root`
    and used a non-existent `pr_created` event_type).
    """
    path = get_settings().project_root / "docs" / "examples" / "session-timeline.json"
    data = json.loads(path.read_text(encoding="utf-8"))

    session = SprintSession.model_validate(data)

    assert all(event.summary for event in session.events)
    event_types = {event.event_type for event in session.events}
    assert "pr_created" not in event_types
    assert "shipped" in event_types
