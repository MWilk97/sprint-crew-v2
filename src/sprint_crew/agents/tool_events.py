from __future__ import annotations

from typing import Any

from sprint_crew.schemas.session import AgentEvent

ToolCallLog = list[dict[str, Any]]


def tool_call_events(agent: str, tool_log: ToolCallLog) -> list[AgentEvent]:
    events: list[AgentEvent] = []
    for index, entry in enumerate(tool_log, start=1):
        tool = str(entry.get("tool", "unknown"))
        ok = bool(entry.get("ok", False))
        events.append(
            AgentEvent(
                agent=agent,
                event_type="tool_call",
                summary=f"{tool} ({'ok' if ok else 'error'})",
                detail={
                    "index": index,
                    "tool": tool,
                    "args": entry.get("args"),
                    "ok": ok,
                    "output_preview": entry.get("output_preview", ""),
                },
            )
        )
    return events
