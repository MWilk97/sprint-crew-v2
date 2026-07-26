from __future__ import annotations

from typing import Any

from sprint_crew.schemas.session import AgentEvent, utc_now_iso

ToolCallLog = list[dict[str, Any]]


def tool_call_event(agent: str, entry: dict[str, Any], index: int) -> AgentEvent:
    """Build one ``tool_call`` event from a tool-log entry (roadmap M4).

    Shared by the live emit path (``_record_tool_call``) and the batch conversion below,
    so both produce identical shapes. ``timestamp`` and ``duration_ms`` are stamped at
    call time by ``_record_tool_call``; when absent (older entries) ``AgentEvent`` defaults
    ``timestamp`` to now and ``duration_ms`` is omitted.
    """
    tool = str(entry.get("tool", "unknown"))
    ok = bool(entry.get("ok", False))
    detail: dict[str, Any] = {
        "index": index,
        "tool": tool,
        "args": entry.get("args"),
        "ok": ok,
        "output_preview": entry.get("output_preview", ""),
    }
    if (duration_ms := entry.get("duration_ms")) is not None:
        detail["duration_ms"] = duration_ms
    return AgentEvent(
        timestamp=entry.get("timestamp") or utc_now_iso(),
        agent=agent,
        event_type="tool_call",
        summary=f"{tool} ({'ok' if ok else 'error'})",
        detail=detail,
        # Carried when this call was already published live, so the node-end persistence
        # can recognise it as already in the timeline (see orchestrator/emitter.py).
        seq=entry.get("seq"),
        # Hundreds of these land per run; ``debug`` is what lets a UI filter them out of the
        # default timeline while a failed call still surfaces at ``warning``.
        level="debug" if ok else "warning",
    )


def tool_call_events(agent: str, tool_log: ToolCallLog) -> list[AgentEvent]:
    return [tool_call_event(agent, entry, index) for index, entry in enumerate(tool_log, start=1)]
