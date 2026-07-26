from __future__ import annotations

import json

import yaml

from sprint_crew.config import get_settings
from sprint_crew.schemas.console import ConsoleSessionStatus
from sprint_crew.schemas.session import EventType, SprintSession


def _openapi() -> dict:
    path = get_settings().project_root / "docs" / "contracts" / "chat-console.openapi.yaml"
    return yaml.safe_load(path.read_text(encoding="utf-8"))


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


def test_openapi_event_vocabulary_matches_the_code() -> None:
    """The FE generates its client from this spec, so a published vocabulary that lags the
    code means unrenderable events. Both M4 and M5 additions had to be backfilled once.
    """
    schemas = _openapi()["components"]["schemas"]

    assert set(schemas["EventType"]["enum"]) == {e.value for e in EventType}
    assert set(schemas["ConsoleSessionStatus"]["enum"]) == {s.value for s in ConsoleSessionStatus}


def test_markdown_contract_does_not_restate_the_event_vocabulary() -> None:
    """The prose copy of the event list in chat-console-api.md fell four milestones behind
    the enum before anyone noticed, because no test read it. The fix was to delete the copy
    and point at the OpenAPI enum; this keeps anyone from reintroducing one.
    """
    path = get_settings().project_root / "docs" / "contracts" / "chat-console-api.md"
    text = path.read_text(encoding="utf-8")

    # A restated list would name most of the enum inline; a pointer names only a few
    # examples. Anything approaching the full vocabulary is a second source of truth.
    inline = {e.value for e in EventType if f"`{e.value}`" in text}
    assert len(inline) < len(EventType) // 2, (
        f"chat-console-api.md appears to restate the event vocabulary ({len(inline)} of "
        f"{len(EventType)} types inline). Cite the OpenAPI EventType enum instead."
    )


def test_openapi_console_session_properties_match_the_model() -> None:
    spec_props = set(_openapi()["components"]["schemas"]["ConsoleSession"]["properties"])
    from sprint_crew.schemas.console import ConsoleSession

    assert spec_props == set(ConsoleSession.model_fields)
