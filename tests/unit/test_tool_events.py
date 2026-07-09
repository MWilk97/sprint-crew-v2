from __future__ import annotations

from sprint_crew.agents.tool_events import tool_call_events


def test_tool_call_events_maps_log_entries() -> None:
    events = tool_call_events(
        "coder",
        [
            {
                "tool": "read_file",
                "args": {"path": "greeter.py"},
                "ok": True,
                "output_preview": "def greet",
            }
        ],
    )
    assert len(events) == 1
    assert events[0].agent == "coder"
    assert events[0].event_type == "tool_call"
    assert events[0].detail is not None
    assert events[0].detail["tool"] == "read_file"
