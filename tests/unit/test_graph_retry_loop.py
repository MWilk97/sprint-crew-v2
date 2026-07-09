from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from sprint_crew.graph.pipeline import build_sprint_graph
from sprint_crew.schemas.change import CodeChange, ReviewOutcome
from sprint_crew.schemas.session import SessionStatus
from sprint_crew.schemas.ticket import JiraTicket, TaskPlan


@pytest.fixture
def graph_state(tmp_path) -> dict:
    ticket = JiraTicket(
        key="DEMO-1",
        summary="Add hello()",
        status="To Do",
        issue_type="Story",
        acceptance_criteria="pytest -q",
    )
    return {
        "session_id": "graph-retry",
        "workspace_root": str(tmp_path),
        "selected_ticket": ticket.model_dump(),
        "attempt": 0,
        "prior_review_feedback": "",
        "events": [],
        "use_real_ship": False,
    }


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
) -> None:
    import subprocess

    subprocess.run(
        ["git", "init"], cwd=graph_state["workspace_root"], check=True, capture_output=True
    )
    graph = build_sprint_graph()
    reviews = [_failing_review(), _passing_review()]

    with (
        patch("sprint_crew.graph.pipeline.ensure_lane", new=AsyncMock()),
        patch("sprint_crew.graph.pipeline.stop_lane", new=AsyncMock()),
        patch(
            "sprint_crew.graph.pipeline.run_tech_lead_validated",
            new=AsyncMock(return_value=(task_plan, "template", [])),
        ),
        patch(
            "sprint_crew.graph.pipeline.run_coder_with_coverage",
            new=AsyncMock(
                return_value=(
                    "handoff",
                    [],
                    MagicMock(satisfied=True, missing=[], unexpected=[], out_of_scope_hits=[]),
                )
            ),
        ),
        patch("sprint_crew.graph.pipeline.gather_workspace_diff", return_value="diff"),
        patch("sprint_crew.graph.pipeline.run_formatter", new=AsyncMock(return_value=code_change)),
        patch("sprint_crew.graph.pipeline.should_invoke_tester", return_value=False),
        patch("sprint_crew.graph.pipeline.run_reviewer", new=AsyncMock(side_effect=reviews)),
        patch("sprint_crew.orchestrator.plan_coverage.subprocess.run") as mock_subprocess,
    ):
        mock_subprocess.return_value.returncode = 0
        mock_subprocess.return_value.stdout = ""
        mock_subprocess.return_value.stderr = ""
        result = await graph.ainvoke(graph_state)

    assert result["status"] == SessionStatus.AWAITING_HUMAN
    assert result["attempt"] == 1
    assert result["review_outcome"]["passed"] is True


@pytest.mark.asyncio
async def test_graph_review_fail_plan_retry_routes_to_tech_lead(
    graph_state: dict,
    task_plan: TaskPlan,
    code_change: CodeChange,
) -> None:
    import subprocess

    subprocess.run(
        ["git", "init"], cwd=graph_state["workspace_root"], check=True, capture_output=True
    )
    graph = build_sprint_graph()
    plan_review = _failing_review().model_copy(
        update={"retry_scope": "plan", "summary": "wrong files"}
    )

    tech_lead_mock = AsyncMock(return_value=(task_plan, "static", []))

    with (
        patch("sprint_crew.graph.pipeline.ensure_lane", new=AsyncMock()),
        patch("sprint_crew.graph.pipeline.stop_lane", new=AsyncMock()),
        patch("sprint_crew.graph.pipeline.run_tech_lead_validated", new=tech_lead_mock),
        patch(
            "sprint_crew.graph.pipeline.run_coder_with_coverage",
            new=AsyncMock(
                return_value=(
                    "handoff",
                    [],
                    MagicMock(satisfied=True, missing=[], unexpected=[], out_of_scope_hits=[]),
                )
            ),
        ),
        patch("sprint_crew.graph.pipeline.gather_workspace_diff", return_value="diff"),
        patch("sprint_crew.graph.pipeline.run_formatter", new=AsyncMock(return_value=code_change)),
        patch("sprint_crew.graph.pipeline.should_invoke_tester", return_value=False),
        patch(
            "sprint_crew.graph.pipeline.run_reviewer",
            new=AsyncMock(side_effect=[plan_review, _passing_review()]),
        ),
        patch("sprint_crew.orchestrator.plan_coverage.subprocess.run") as mock_subprocess,
    ):
        mock_subprocess.return_value.returncode = 0
        mock_subprocess.return_value.stdout = ""
        mock_subprocess.return_value.stderr = ""
        result = await graph.ainvoke(graph_state)

    assert tech_lead_mock.await_count >= 2
    assert result["plan_retries"] == 1


@pytest.mark.asyncio
async def test_graph_exhausted_retries_end_in_failed(
    graph_state: dict,
    task_plan: TaskPlan,
    code_change: CodeChange,
) -> None:
    import subprocess

    subprocess.run(
        ["git", "init"], cwd=graph_state["workspace_root"], check=True, capture_output=True
    )
    graph_state["attempt"] = 4
    graph = build_sprint_graph()

    with (
        patch("sprint_crew.graph.pipeline.ensure_lane", new=AsyncMock()),
        patch("sprint_crew.graph.pipeline.stop_lane", new=AsyncMock()),
        patch(
            "sprint_crew.graph.pipeline.run_tech_lead_validated",
            new=AsyncMock(return_value=(task_plan, "template", [])),
        ),
        patch(
            "sprint_crew.graph.pipeline.run_coder_with_coverage",
            new=AsyncMock(
                return_value=(
                    "handoff",
                    [],
                    MagicMock(satisfied=True, missing=[], unexpected=[], out_of_scope_hits=[]),
                )
            ),
        ),
        patch("sprint_crew.graph.pipeline.gather_workspace_diff", return_value="diff"),
        patch("sprint_crew.graph.pipeline.run_formatter", new=AsyncMock(return_value=code_change)),
        patch("sprint_crew.graph.pipeline.should_invoke_tester", return_value=False),
        patch(
            "sprint_crew.graph.pipeline.run_reviewer", new=AsyncMock(return_value=_failing_review())
        ),
        patch("sprint_crew.orchestrator.plan_coverage.subprocess.run") as mock_subprocess,
        patch("sprint_crew.graph.pipeline.get_settings") as settings_mock,
    ):
        settings_mock.return_value.max_review_retries = 4
        mock_subprocess.return_value.returncode = 0
        mock_subprocess.return_value.stdout = ""
        mock_subprocess.return_value.stderr = ""
        result = await graph.ainvoke(graph_state)

    assert result["status"] == SessionStatus.FAILED


@pytest.mark.asyncio
async def test_graph_coverage_incomplete_blocks_ship_despite_passing_review(
    graph_state: dict,
    task_plan: TaskPlan,
    code_change: CodeChange,
) -> None:
    import subprocess

    subprocess.run(
        ["git", "init"], cwd=graph_state["workspace_root"], check=True, capture_output=True
    )
    graph = build_sprint_graph()
    unsatisfied = MagicMock(
        satisfied=False, missing=["greeter.py"], unexpected=[], out_of_scope_hits=[]
    )

    with (
        patch("sprint_crew.graph.pipeline.ensure_lane", new=AsyncMock()),
        patch("sprint_crew.graph.pipeline.stop_lane", new=AsyncMock()),
        patch(
            "sprint_crew.graph.pipeline.run_tech_lead_validated",
            new=AsyncMock(return_value=(task_plan, "template", [])),
        ),
        patch(
            "sprint_crew.graph.pipeline.run_coder_with_coverage",
            new=AsyncMock(return_value=("handoff", [], unsatisfied)),
        ),
        patch("sprint_crew.graph.pipeline.gather_workspace_diff", return_value="diff"),
        patch("sprint_crew.graph.pipeline.run_formatter", new=AsyncMock(return_value=code_change)),
        patch("sprint_crew.graph.pipeline.should_invoke_tester", return_value=False),
        patch(
            "sprint_crew.graph.pipeline.run_reviewer", new=AsyncMock(return_value=_passing_review())
        ),
        patch("sprint_crew.orchestrator.plan_coverage.subprocess.run") as mock_subprocess,
        patch("sprint_crew.graph.pipeline.get_settings") as settings_mock,
    ):
        settings_mock.return_value.max_review_retries = 4
        mock_subprocess.return_value.returncode = 0
        mock_subprocess.return_value.stdout = ""
        mock_subprocess.return_value.stderr = ""
        result = await graph.ainvoke(graph_state)

    assert result["status"] != SessionStatus.AWAITING_HUMAN
    retry_events = [
        e
        for e in result["events"]
        if getattr(e, "event_type", e.get("event_type") if isinstance(e, dict) else None)
        == "retry_prepared"
    ]
    assert retry_events
