from __future__ import annotations

import time
from unittest.mock import AsyncMock, patch

import pytest
from pydantic_ai.exceptions import ModelAPIError
from tests.helpers.ticket_fixtures import complex_api_ticket, greeter_ticket

from sprint_crew.agents import tech_lead as tech_lead_module
from sprint_crew.agents.tech_lead import (
    _append_programmatic_semantic_search,
    _semantic_retrieval_satisfied,
    run_tech_lead,
)
from sprint_crew.agents.tech_lead_planning import run_tech_lead_validated
from sprint_crew.config import get_settings
from sprint_crew.orchestrator.repo_context import gather_repo_context, paths_from_ticket
from sprint_crew.schemas.ticket import JiraTicket, PlanStep, TaskPlan
from sprint_crew.vector.scope import IndexScope
from sprint_crew.vector.search import SearchHit


def _scope() -> IndexScope:
    return IndexScope(repo_key="k", shared="shared", overlay="overlay")


def test_paths_from_ticket_extracts_greeter_py() -> None:
    ticket = JiraTicket(
        key="DEMO-1",
        summary="Add hello() to greeter.py",
        description="Implement hello() in greeter.py with pytest.",
        status="To Do",
        issue_type="Story",
        acceptance_criteria="pytest -q tests/test_greeter.py passes",
    )
    paths = paths_from_ticket(ticket)
    assert "greeter.py" in paths
    assert "tests/test_greeter.py" in paths


def test_gather_repo_context_includes_git_status_and_greeter(tmp_workspace) -> None:
    (tmp_workspace / "greeter.py").write_text(
        "def hello():\n    return 'hello'\n", encoding="utf-8"
    )
    ticket = JiraTicket(
        key="DEMO-1",
        summary="Add hello() to greeter.py",
        description="Implement hello() returning 'hello'.",
        status="To Do",
        issue_type="Story",
        acceptance_criteria="- tests pass",
    )
    context = gather_repo_context(tmp_workspace, ticket)
    assert "=== git status ===" in context
    assert "=== directory listing" in context
    assert "=== read_file: greeter.py ===" in context
    assert "hello" in context


def test_enrich_repo_context_appends_semantic_section(tmp_workspace, monkeypatch) -> None:
    monkeypatch.setenv("VECTOR_INDEX_ENABLED", "true")
    from sprint_crew.config import get_settings

    get_settings.cache_clear()

    monkeypatch.setattr(
        "sprint_crew.orchestrator.repo_context.should_index_workspace",
        lambda *a, **k: True,
    )
    monkeypatch.setattr(
        "sprint_crew.orchestrator.repo_context.semantic_search",
        lambda *a, **k: [
            SearchHit(
                path="auth.py",
                start_line=1,
                end_line=5,
                score=0.88,
                chunk_kind="code",
                snippet="def auth(): ...",
            )
        ],
    )

    from sprint_crew.orchestrator.repo_context import enrich_repo_context

    text = enrich_repo_context(tmp_workspace, _scope(), "authentication middleware")
    assert "=== repo_manifest" in text
    assert "=== pre_search" in text
    assert text.count("auth.py") == 1
    assert "auth.py" in text


def test_enrich_repo_context_with_hits_returns_hit_list(tmp_workspace, monkeypatch) -> None:
    monkeypatch.setenv("VECTOR_INDEX_ENABLED", "true")
    from sprint_crew.config import get_settings

    get_settings.cache_clear()

    hit = SearchHit(
        path="auth.py",
        start_line=1,
        end_line=5,
        score=0.88,
        chunk_kind="code",
        snippet="def auth(): ...",
    )
    monkeypatch.setattr(
        "sprint_crew.orchestrator.repo_context.should_index_workspace",
        lambda *a, **k: True,
    )
    monkeypatch.setattr(
        "sprint_crew.orchestrator.repo_context.semantic_search",
        lambda *a, **k: [hit],
    )

    from sprint_crew.orchestrator.repo_context import (
        enrich_repo_context_with_hits,
        pre_search_agent_event,
    )

    text, hits = enrich_repo_context_with_hits(tmp_workspace, _scope(), "authentication")
    assert hits == [hit]
    assert "auth.py" in text
    event = pre_search_agent_event("authentication", hits)
    assert event.event_type == "pre_search"
    assert event.detail["hits"] == 1


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
    hit = SearchHit(
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
            collections=("overlay", "shared"),
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
            return_value=TaskPlan(**plan_json),
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


@pytest.mark.asyncio
async def test_run_tech_lead_validated_retries_once_on_invalid_commands(tmp_path) -> None:
    (tmp_path / "greeter.py").write_text("pass\n", encoding="utf-8")
    ticket = JiraTicket(
        key="DEMO-1",
        summary="Add hello()",
        status="To Do",
        issue_type="Story",
    )
    bad_plan = TaskPlan(
        ticket_key="DEMO-1",
        summary="Add hello()",
        steps=[PlanStep(description="edit", files=["greeter.py"])],
        acceptance_tests=["pytest -q passes"],
    )
    good_plan = bad_plan.model_copy(update={"acceptance_tests": ["pytest -q"]})
    run_mock = AsyncMock(
        side_effect=[(bad_plan, "static"), (good_plan, "static")],
    )

    with patch("sprint_crew.agents.tech_lead_planning.run_tech_lead", new=run_mock):
        plan, mode, _tool_log = await run_tech_lead_validated(ticket, tmp_path)

    assert plan.acceptance_tests == ["pytest -q"]
    assert mode == "static"
    assert run_mock.await_count == 2
    second_call_kwargs = run_mock.await_args_list[1].kwargs
    assert "validation failed" in second_call_kwargs["prior_review_feedback"].lower()


@pytest.mark.asyncio
async def test_run_tech_lead_validated_template_fallback_after_two_failures(
    tmp_path, monkeypatch
) -> None:
    ticket = JiraTicket(
        key="DEMO-1",
        summary="Add hello() to greeter.py",
        description="Implement hello() returning 'hello'.",
        status="To Do",
        issue_type="Story",
        acceptance_criteria="- Unit tests pass\n- hello() returns 'hello'",
    )
    bad_plan = TaskPlan(
        ticket_key="DEMO-1",
        summary="Add hello()",
        steps=[PlanStep(description="edit", files=["greeter.py"])],
        acceptance_tests=["pytest -q passes"],
    )
    run_mock = AsyncMock(return_value=(bad_plan, "static"))

    # Pin plan retries so the attempt count (max(3, MAX_PLAN_RETRIES + 2)) is
    # independent of the production default.
    monkeypatch.setenv("MAX_PLAN_RETRIES", "1")
    get_settings.cache_clear()
    try:
        with patch("sprint_crew.agents.tech_lead_planning.run_tech_lead", new=run_mock):
            plan, mode, _tool_log = await run_tech_lead_validated(ticket, tmp_path)
    finally:
        get_settings.cache_clear()

    assert mode == "template_fallback"
    assert plan.ticket_key == "DEMO-1"
    assert run_mock.await_count == 3


@pytest.mark.asyncio
async def test_run_tech_lead_validated_returns_template_mode_for_greeter(tmp_path) -> None:
    ticket = greeter_ticket(summary="Add hello() to greeter.py")
    plan, mode, _tool_log = await run_tech_lead_validated(ticket, tmp_path)
    assert mode == "template"
    assert plan.ticket_key == "DEMO-1"


@pytest.mark.asyncio
async def test_run_tech_lead_complex_uses_tool_loop(tmp_path) -> None:
    ticket = complex_api_ticket()
    expected = TaskPlan(
        ticket_key="DEMO-2",
        summary="api",
        steps=[PlanStep(description="routes", files=["routes.py"])],
        files_to_touch=["routes.py"],
        acceptance_tests=["pytest -q"],
    )
    with (
        patch.object(
            tech_lead_module,
            "run_tech_lead_loop",
            new=AsyncMock(return_value="handoff text"),
        ) as loop_mock,
        patch.object(
            tech_lead_module,
            "_repo_context_for_ticket",
            return_value="REAL_REPO_CONTEXT",
        ),
        patch.object(
            tech_lead_module,
            "_structured_plan_from_context",
            new=AsyncMock(return_value=expected),
        ) as structured_mock,
        patch(
            "sprint_crew.orchestrator.template_plan.build_template_task_plan_validated",
            side_effect=RuntimeError("skip template for complex"),
        ),
    ):
        plan, mode = await tech_lead_module.run_tech_lead(ticket, tmp_path)

    loop_mock.assert_awaited_once()
    structured_mock.assert_called_once()
    call_kwargs = structured_mock.call_args.kwargs
    assert call_kwargs["repo_context"] == "REAL_REPO_CONTEXT"
    assert call_kwargs["planning_handoff"] == "handoff text"
    assert mode == "tool_loop"
    assert plan.ticket_key == "DEMO-2"


def _fallback_ticket() -> JiraTicket:
    """Ticket the deterministic template plan can cover, so the fallback path is reachable."""
    return JiraTicket(
        key="DEMO-1",
        summary="Add hello() to greeter.py",
        description="Implement hello() returning 'hello'.",
        status="To Do",
        issue_type="Story",
        acceptance_criteria="- Unit tests pass\n- hello() returns 'hello'",
    )


@pytest.mark.asyncio
async def test_run_tech_lead_validated_falls_back_when_lane_keeps_timing_out(tmp_path) -> None:
    run_mock = AsyncMock(side_effect=ModelAPIError(model_name="work", message="Request timed out."))

    with patch("sprint_crew.agents.tech_lead_planning.run_tech_lead", new=run_mock):
        plan, mode, _tool_log = await run_tech_lead_validated(_fallback_ticket(), tmp_path)

    assert mode == "template_fallback"
    assert plan.ticket_key == "DEMO-1"
    # One retry for a transient blip, then the deterministic plan — not the whole ladder.
    assert run_mock.await_count == 2


@pytest.mark.asyncio
async def test_run_tech_lead_validated_recovers_from_one_lane_timeout(tmp_path) -> None:
    (tmp_path / "greeter.py").write_text("pass\n", encoding="utf-8")
    good_plan = TaskPlan(
        ticket_key="DEMO-1",
        summary="Add hello()",
        steps=[PlanStep(description="edit", files=["greeter.py"])],
        acceptance_tests=["pytest -q"],
    )
    run_mock = AsyncMock(
        side_effect=[
            ModelAPIError(model_name="work", message="Request timed out."),
            (good_plan, "static"),
        ],
    )

    with patch("sprint_crew.agents.tech_lead_planning.run_tech_lead", new=run_mock):
        plan, mode, _tool_log = await run_tech_lead_validated(_fallback_ticket(), tmp_path)

    assert mode == "static"
    assert plan.acceptance_tests == ["pytest -q"]
    assert run_mock.await_count == 2


@pytest.mark.asyncio
async def test_run_tech_lead_validated_stops_ladder_once_deadline_passes(tmp_path) -> None:
    bad_plan = TaskPlan(
        ticket_key="DEMO-1",
        summary="Add hello()",
        steps=[PlanStep(description="edit", files=["greeter.py"])],
        acceptance_tests=["pytest -q passes"],
    )
    run_mock = AsyncMock(return_value=(bad_plan, "static"))

    with patch("sprint_crew.agents.tech_lead_planning.run_tech_lead", new=run_mock):
        plan, mode, _tool_log = await run_tech_lead_validated(
            _fallback_ticket(), tmp_path, deadline_epoch=time.time() - 1
        )

    assert mode == "template_fallback"
    assert plan.ticket_key == "DEMO-1"
    # The elapsed budget is only consulted between attempts, so exactly one pass runs.
    assert run_mock.await_count == 1
