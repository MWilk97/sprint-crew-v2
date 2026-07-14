from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from sprint_crew.agents.tech_lead import (
    _append_programmatic_semantic_search,
    _semantic_retrieval_satisfied,
    run_tech_lead,
)
from sprint_crew.schemas.ticket import JiraTicket


def test_semantic_retrieval_satisfied_with_pre_search_hits() -> None:
    assert _semantic_retrieval_satisfied([], pre_search_hits=3) is True
    assert _semantic_retrieval_satisfied([], pre_search_hits=0) is False


def test_semantic_retrieval_satisfied_with_tool_log() -> None:
    log = [{"tool": "semantic_search", "args": {"query": "ferry"}}]
    assert _semantic_retrieval_satisfied(log, pre_search_hits=0) is True


def test_append_programmatic_semantic_search_records_tool_log() -> None:
    ticket = JiraTicket(
        key="DEMO-1",
        summary="queue via ferry",
        description="wire queue worker",
        status="To Do",
        issue_type="Story",
        acceptance_criteria="pytest -q",
    )
    log: list[dict] = []
    hit = __import__("sprint_crew.vector.search", fromlist=["SearchHit"]).SearchHit(
        path="src/messaging/ferry.py",
        start_line=1,
        end_line=5,
        score=0.9,
        chunk_kind="code",
        snippet="class Ferry",
    )
    with patch(
        "sprint_crew.vector.search.semantic_search",
        return_value=[hit],
    ):
        result = _append_programmatic_semantic_search(
            "handoff text",
            session_id="sess-1",
            ticket=ticket,
            tool_log=log,
        )
    assert "ferry.py" in result
    assert log[0]["tool"] == "semantic_search"


@pytest.mark.asyncio
async def test_run_tech_lead_skips_nudge_when_pre_search_hits(tmp_path) -> None:
    ticket = JiraTicket(
        key="DEMO-1",
        summary="Complex multi-file refactor across services",
        description="Update queue worker and retry policy integration points.",
        status="To Do",
        issue_type="Story",
        acceptance_criteria="pytest -q tests/test_ferry_queue.py",
    )
    plan_json = {
        "ticket_key": "DEMO-1",
        "summary": "plan",
        "steps": [{"description": "step", "files": ["src/messaging/queue_worker.py"]}],
        "files_to_touch": ["src/messaging/queue_worker.py"],
        "acceptance_tests": ["pytest -q tests/test_ferry_queue.py"],
        "out_of_scope": [],
    }
    loop_mock = AsyncMock(return_value="exploration handoff")
    with (
        patch(
            "sprint_crew.orchestrator.template_plan.build_template_task_plan_validated",
            side_effect=RuntimeError("no template"),
        ),
        patch("sprint_crew.agents.tech_lead.tech_lead_mode", return_value="tool_loop"),
        patch("sprint_crew.agents.tech_lead.run_tech_lead_loop", loop_mock),
        patch(
            "sprint_crew.agents.tech_lead.structured_completion",
            return_value=__import__("sprint_crew.schemas.ticket", fromlist=["TaskPlan"]).TaskPlan(
                **plan_json
            ),
        ),
        patch(
            "sprint_crew.orchestrator.repo_context.should_use_vector",
            return_value=True,
        ),
        patch(
            "sprint_crew.agents.tech_lead._repo_context_for_ticket",
            return_value="repo context",
        ),
    ):
        await run_tech_lead(
            ticket,
            tmp_path,
            session_id="sess-1",
            pre_search_hit_count=5,
        )
    assert loop_mock.await_count == 1
