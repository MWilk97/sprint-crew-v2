from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from sprint_crew.graph.pipeline import build_sprint_graph
from sprint_crew.schemas.change import CodeChange, ReviewOutcome
from sprint_crew.schemas.session import SessionStatus
from sprint_crew.schemas.ticket import TaskPlan


@pytest.fixture
def graph_state(base_state: dict) -> dict:
    graph_state = dict(base_state)
    graph_state["session_id"] = "graph-retry"
    return graph_state


def _passing_review() -> ReviewOutcome:
    return ReviewOutcome(
        ticket_key="DEMO-1",
        passed=True,
        summary="ok",
        tests_passed=True,
    )


def _failing_review() -> ReviewOutcome:
    return ReviewOutcome(
        ticket_key="DEMO-1",
        passed=False,
        summary="fix hello()",
        tests_passed=False,
        retry_scope="code",
    )


@pytest.mark.asyncio
async def test_graph_review_fail_then_code_retry_reaches_ship(
    graph_state: dict,
    task_plan: TaskPlan,
    code_change: CodeChange,
    graph_run_mocks,
) -> None:
    import subprocess

    subprocess.run(
        ["git", "init"], cwd=graph_state["workspace_root"], check=True, capture_output=True
    )
    graph = build_sprint_graph()
    reviews = [_failing_review(), _passing_review()]

    with graph_run_mocks(
        tech_lead_result=(task_plan, "template", []),
        coder_result=(
            "handoff",
            [],
            MagicMock(satisfied=True, missing=[], unexpected=[], out_of_scope_hits=[]),
        ),
        formatter_result=code_change,
        should_invoke_tester=False,
        reviewer=AsyncMock(side_effect=reviews),
    ):
        result = await graph.ainvoke(graph_state)

    assert result["status"] == SessionStatus.AWAITING_HUMAN
    assert result["attempt"] == 1
    assert result["review_outcome"]["passed"] is True


@pytest.mark.asyncio
async def test_graph_review_fail_plan_retry_routes_to_tech_lead(
    graph_state: dict,
    task_plan: TaskPlan,
    code_change: CodeChange,
    graph_run_mocks,
) -> None:
    import subprocess

    subprocess.run(
        ["git", "init"], cwd=graph_state["workspace_root"], check=True, capture_output=True
    )
    graph = build_sprint_graph()
    plan_review = _failing_review().model_copy(
        update={"retry_scope": "plan", "summary": "wrong files"}
    )

    with graph_run_mocks(
        tech_lead_result=(task_plan, "static", []),
        coder_result=(
            "handoff",
            [],
            MagicMock(satisfied=True, missing=[], unexpected=[], out_of_scope_hits=[]),
        ),
        formatter_result=code_change,
        should_invoke_tester=False,
        reviewer=AsyncMock(side_effect=[plan_review, _passing_review()]),
    ) as mocks:
        result = await graph.ainvoke(graph_state)

    assert mocks["tech_lead"].await_count >= 2
    assert result["plan_retries"] == 1


@pytest.mark.asyncio
async def test_graph_exhausted_retries_end_in_failed(
    graph_state: dict,
    task_plan: TaskPlan,
    code_change: CodeChange,
    graph_run_mocks,
) -> None:
    import subprocess

    subprocess.run(
        ["git", "init"], cwd=graph_state["workspace_root"], check=True, capture_output=True
    )
    graph_state["attempt"] = 4
    graph = build_sprint_graph()

    with graph_run_mocks(
        tech_lead_result=(task_plan, "template", []),
        coder_result=(
            "handoff",
            [],
            MagicMock(satisfied=True, missing=[], unexpected=[], out_of_scope_hits=[]),
        ),
        formatter_result=code_change,
        should_invoke_tester=False,
        reviewer=AsyncMock(return_value=_failing_review()),
        settings_overrides={"max_review_retries": 4},
    ):
        result = await graph.ainvoke(graph_state)

    assert result["status"] == SessionStatus.FAILED


@pytest.mark.asyncio
async def test_graph_coverage_incomplete_blocks_ship_despite_passing_review(
    graph_state: dict,
    task_plan: TaskPlan,
    code_change: CodeChange,
    graph_run_mocks,
) -> None:
    import subprocess

    subprocess.run(
        ["git", "init"], cwd=graph_state["workspace_root"], check=True, capture_output=True
    )
    graph = build_sprint_graph()
    unsatisfied = MagicMock(
        satisfied=False, missing=["greeter.py"], unexpected=[], out_of_scope_hits=[]
    )

    with graph_run_mocks(
        tech_lead_result=(task_plan, "template", []),
        coder_result=("handoff", [], unsatisfied),
        formatter_result=code_change,
        should_invoke_tester=False,
        reviewer=AsyncMock(return_value=_passing_review()),
        settings_overrides={"max_review_retries": 4},
    ):
        result = await graph.ainvoke(graph_state)

    assert result["status"] != SessionStatus.AWAITING_HUMAN
    retry_events = [
        e
        for e in result["events"]
        if getattr(e, "event_type", e.get("event_type") if isinstance(e, dict) else None)
        == "retry_prepared"
    ]
    assert retry_events
