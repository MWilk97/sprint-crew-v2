from __future__ import annotations

import json
import os
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from sprint_crew.config import get_settings
from sprint_crew.schemas.session import SprintSession
from tests.helpers.cycle_assertions import (
    assert_cycle_passed,
    count_semantic_retrieval_events,
    count_tool_call_events,
    index_chunks_from_session,
    semantic_retrieval_diagnostics,
)
from tests.helpers.session_metrics import planning_mode_from_session
from tests.helpers.vector_live import wait_vector_healthy

_ROOT = Path(__file__).resolve().parents[2]
_LANE_CTL = _ROOT / "scripts" / "lane-ctl.sh"


def skip_unless_vector_agent_live() -> None:
    if os.environ.get("VECTOR_AGENT_LIVE", "").strip() not in {"1", "true", "yes"}:
        pytest.skip("vector agent live tests require VECTOR_AGENT_LIVE=1")


def trap_strict_mode() -> bool:
    """Traps hard-fail by default; a trap the agent falls for is a real failure.

    Set ``VECTOR_TRAP_SOFT=1`` for exploratory benchmark runs that should still
    emit the trap report but not go red (legacy ``VECTOR_TRAP_STRICT`` is honored
    as an explicit force-strict override).
    """
    if os.environ.get("VECTOR_TRAP_STRICT", "").strip() in {"1", "true", "yes"}:
        return True
    return os.environ.get("VECTOR_TRAP_SOFT", "").strip() not in {"1", "true", "yes"}


def setup_vector_agent_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VECTOR_INDEX_ENABLED", "true")
    monkeypatch.setenv("MAX_TECHLEAD_TURNS", "24")
    monkeypatch.setenv("MAX_CODER_TURNS", "64")
    monkeypatch.setenv("MAX_TESTER_TURNS", "80")
    get_settings.cache_clear()


def start_vector_stack() -> None:
    settings = get_settings()
    subprocess.run([str(_LANE_CTL), "stop", "all"], check=False)
    subprocess.run([str(_LANE_CTL), "start", "vector"], check=True)
    wait_vector_healthy(
        qdrant_url=settings.qdrant_url,
        embed_url=settings.embed_url.replace("/v1", ""),
    )


def count_write_tool_events(session: SprintSession) -> int:
    write_tools = {"write_file", "apply_patch"}
    return sum(
        1
        for event in session.events
        if event.event_type == "tool_call" and (event.detail or {}).get("tool") in write_tools
    )


def collect_capability_metrics(
    session: SprintSession,
    *,
    tier: str,
    story_id: str,
    duration_s: float,
) -> dict[str, Any]:
    total_tools = count_tool_call_events(session)
    write_tools = count_write_tool_events(session)
    mode = planning_mode_from_session(session)
    chunks = index_chunks_from_session(session)
    semantic_calls = count_semantic_retrieval_events(session)
    return {
        "tier": tier,
        "story_id": story_id,
        "duration_s": round(duration_s, 2),
        "session_id": session.session_id,
        "ticket_key": session.ticket_key,
        "session_status": session.status.value,
        "planning_mode": mode,
        "index_chunks": chunks,
        "semantic_tool_calls": semantic_calls,
        "total_tool_calls": total_tools,
        "write_tool_calls": write_tools,
        "write_ratio": round(write_tools / total_tools, 3) if total_tools else 0.0,
        "semantic_diagnostics": semantic_retrieval_diagnostics(session),
        "failure_class": failure_class_from_session(session),
    }


def failure_class_from_session(session: SprintSession) -> str | None:
    for event in reversed(session.events):
        detail = event.detail or {}
        if event.agent == "merge_gate" and event.event_type == "gate_result":
            block = detail.get("block_reason")
            if isinstance(block, str) and block:
                return block
        if event.event_type == "skipped" and event.agent == "tester":
            summary = event.summary or ""
            if "Source/build failure" in summary:
                return "source_build_failure"
    review = session.review_outcome
    if review is not None and not review.tests_passed:
        return "tests_failed"
    return None


def assert_capability_cycle(
    session: SprintSession,
    *,
    workspace: Path,
    test_target: str,
) -> None:
    assert_cycle_passed(session, workspace=workspace, test_target=test_target)
    mode = planning_mode_from_session(session)
    assert mode in {"tool_loop", "static", "template_fallback"}, f"unexpected mode={mode}"
    chunks = index_chunks_from_session(session)
    assert chunks is not None and chunks > 0
    semantic_calls = count_semantic_retrieval_events(session)
    assert semantic_calls >= 1, semantic_retrieval_diagnostics(session)


def write_capability_report(payload: dict, *, results_dir: Path | None = None) -> Path:
    root = get_settings().project_root
    out_dir = results_dir or (root / "benchmarks" / "results")
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    story_id = payload.get("story_id", "unknown")
    path = out_dir / f"capability_{story_id}_{stamp}.json"
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return path


def write_trap_report(payload: dict, *, results_dir: Path | None = None) -> Path:
    root = get_settings().project_root
    out_dir = results_dir or (root / "benchmarks" / "results")
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    path = out_dir / f"trap_{stamp}.json"
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return path


def last_gate_result(session: SprintSession) -> dict[str, Any]:
    for event in reversed(session.events):
        if event.agent == "merge_gate" and event.event_type == "gate_result":
            return event.detail or {}
    return {}
