from __future__ import annotations

import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from tests.helpers.agent_live_tickets import greeter_ticket

from sprint_crew.config import Role
from sprint_crew.graph.pipeline import (
    build_sprint_graph,
    route_after_gate,
    route_after_plan,
    route_after_retry,
)
from sprint_crew.schemas.change import CodeChange, ReviewOutcome
from sprint_crew.schemas.session import SessionStatus
from sprint_crew.schemas.ticket import TaskPlan


@pytest.mark.asyncio
async def test_graph_happy_path_mocked(
    tmp_path,
    base_state: dict,
    task_plan: TaskPlan,
    code_change: CodeChange,
    passing_review: ReviewOutcome,
    graph_run_mocks,
) -> None:
    import subprocess

    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
    base_state["workspace_root"] = str(tmp_path)
    graph = build_sprint_graph()

    with graph_run_mocks(
        tech_lead_result=(task_plan, "static", []),
        coder_result=(
            "handoff",
            [],
            MagicMock(satisfied=True, missing=[], unexpected=[], out_of_scope_hits=[]),
            "",
            False,
        ),
        formatter_result=code_change,
        reviewer=AsyncMock(return_value=passing_review),
    ) as mocks:
        result = await graph.ainvoke(base_state)

    assert result["status"] == SessionStatus.AWAITING_HUMAN
    assert result["task_plan"]["ticket_key"] == "DEMO-1"
    assert result["review_outcome"]["passed"] is True
    review_stops = [call.args[0] for call in mocks["stop_lane"].await_args_list if call.args]
    assert Role.WORK in review_stops
    assert mocks["ensure_lane"].await_count >= 1


def test_graph_init_routes_directly_to_tech_lead_plan() -> None:
    graph = build_sprint_graph()
    node_names = set(graph.nodes.keys())
    assert "techLeadPlan" in node_names
    assert "templateTechLead" not in node_names
    assert "skipTechLead" not in node_names


@pytest.mark.asyncio
async def test_graph_simple_ticket_uses_template_via_tech_lead(
    tmp_path,
    base_state: dict,
    code_change: CodeChange,
    passing_review: ReviewOutcome,
    graph_run_mocks,
) -> None:
    import subprocess

    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
    base_state["workspace_root"] = str(tmp_path)
    base_state["selected_ticket"] = greeter_ticket().model_dump()
    graph = build_sprint_graph()

    with graph_run_mocks(
        coder_result=(
            "handoff",
            [],
            MagicMock(satisfied=True, missing=[], unexpected=[], out_of_scope_hits=[]),
            "",
            False,
        ),
        formatter_result=code_change,
        reviewer=AsyncMock(return_value=passing_review),
    ):
        result = await graph.ainvoke(base_state)

    assert result["task_plan"]["ticket_key"] == "DEMO-1"
    assert result["status"] == SessionStatus.AWAITING_HUMAN
    plan_events = [
        e
        for e in result["events"]
        if getattr(e, "agent", e.get("agent") if isinstance(e, dict) else None) == "tech_lead"
        and getattr(e, "event_type", e.get("event_type") if isinstance(e, dict) else None)
        == "plan_created"
    ]
    assert plan_events
    last = plan_events[-1]
    detail = last.detail if hasattr(last, "detail") else last["detail"]
    assert detail is not None
    assert detail["mode"] == "template"


def test_graph_has_direct_init_to_tech_lead_edge() -> None:
    """All tickets enter techLeadPlan; mode selection is internal."""
    graph = build_sprint_graph()
    node_names = set(graph.nodes.keys())
    assert "templateTechLead" not in node_names


@pytest.mark.asyncio
async def test_test_implement_skips_acceptance_when_coder_verified(
    base_state: dict,
    task_plan: TaskPlan,
    code_change: CodeChange,
) -> None:
    from sprint_crew.graph.pipeline import test_implement

    base_state["task_plan"] = task_plan.model_dump()
    base_state["code_change"] = code_change.model_dump()
    base_state["tests_run_this_cycle"] = True
    base_state["acceptance_test_output"] = "pytest ok"

    with patch(
        "sprint_crew.graph.pipeline.run_acceptance_tests",
    ) as run_ac:
        await test_implement(base_state)  # type: ignore[arg-type]

    run_ac.assert_not_called()


@pytest.mark.asyncio
async def test_test_implement_keeps_work_lane_warm_after_reporter(
    base_state: dict,
    task_plan: TaskPlan,
    code_change: CodeChange,
) -> None:
    from sprint_crew.graph.pipeline import test_implement

    base_state["task_plan"] = task_plan.model_dump()
    base_state["code_change"] = code_change.model_dump()
    lane_events: list[str] = []

    async def track_stop(role: Role) -> None:
        lane_events.append(f"stop:{role.value}")

    with (
        patch("sprint_crew.graph.pipeline.stop_lane", side_effect=track_stop),
        patch("sprint_crew.graph.pipeline.ensure_lane", new=AsyncMock()),
        patch(
            "sprint_crew.graph.pipeline.run_tester_loop",
            new=AsyncMock(return_value=("handoff", [])),
        ),
        patch("sprint_crew.graph.pipeline.gather_workspace_diff", return_value="diff"),
        patch("sprint_crew.graph.pipeline.run_tester_reporter", new=AsyncMock(return_value=None)),
        patch("sprint_crew.graph.pipeline.run_acceptance_tests", return_value=("", False)),
        patch("sprint_crew.graph.pipeline.should_invoke_tester", return_value=True),
    ):
        await test_implement(base_state)  # type: ignore[arg-type]

    assert lane_events.count("stop:work") == 1
    assert lane_events[-1] == "stop:coding"


def test_route_after_plan_fails_on_aborted_plan() -> None:
    assert route_after_plan({"status": SessionStatus.FAILED}) == "failed"  # type: ignore[arg-type]
    assert route_after_plan({"status": SessionStatus.RUNNING}) == "code"  # type: ignore[arg-type]


def test_route_after_gate_rejects_incomplete_coverage(passing_review: ReviewOutcome) -> None:
    state = {
        "review_outcome": passing_review.model_dump(),
        "attempt": 0,
        "plan_coverage": {"satisfied": False, "missing": ["greeter.py"]},
    }
    assert route_after_gate(state) == "retry"  # type: ignore[arg-type]


def test_route_after_retry_downgrades_excess_plan_retries(passing_review: ReviewOutcome) -> None:
    state = {
        "retry_scope": "plan",
        "review_outcome": passing_review.model_dump(),
        "plan_retries": 2,
    }
    with patch("sprint_crew.graph.pipeline.get_settings") as settings_mock:
        settings_mock.return_value.max_plan_retries = 1
        assert route_after_retry(state) == "code"  # type: ignore[arg-type]


def test_route_after_retry_uses_state_retry_scope(passing_review: ReviewOutcome) -> None:
    state = {
        "retry_scope": "plan",
        "review_outcome": passing_review.model_copy(
            update={"passed": True, "retry_scope": "code"}
        ).model_dump(),
        "plan_retries": 0,
    }
    assert route_after_retry(state) == "plan"  # type: ignore[arg-type]


def test_route_after_gate_branches(passing_review: ReviewOutcome) -> None:
    accepted = {"review_outcome": passing_review.model_dump(), "attempt": 0}
    assert route_after_gate(accepted) == "ship"  # type: ignore[arg-type]

    rejected = {
        "review_outcome": passing_review.model_copy(update={"passed": False}).model_dump(),
        "attempt": 0,
    }
    assert route_after_gate(rejected) == "retry"  # type: ignore[arg-type]

    exhausted = {
        "review_outcome": passing_review.model_copy(update={"passed": False}).model_dump(),
        "attempt": 99,
    }
    assert route_after_gate(exhausted) == "failed"  # type: ignore[arg-type]


def test_route_after_gate_fails_fast_on_coverage_stall(passing_review: ReviewOutcome) -> None:
    stalled = {
        "review_outcome": passing_review.model_copy(update={"passed": False}).model_dump(),
        "attempt": 1,
        "coverage_stall_count": 2,
    }
    assert route_after_gate(stalled) == "failed"  # type: ignore[arg-type]


def test_route_after_gate_retries_below_stall_threshold(passing_review: ReviewOutcome) -> None:
    not_yet = {
        "review_outcome": passing_review.model_copy(update={"passed": False}).model_dump(),
        "attempt": 1,
        "coverage_stall_count": 1,
    }
    assert route_after_gate(not_yet) == "retry"  # type: ignore[arg-type]


def test_route_after_plan_fails_fast_on_deadline() -> None:
    past = {"status": SessionStatus.RUNNING, "deadline_epoch": time.time() - 1}
    assert route_after_plan(past) == "failed"  # type: ignore[arg-type]


def test_route_after_gate_fails_fast_on_deadline(passing_review: ReviewOutcome) -> None:
    state = {
        "review_outcome": passing_review.model_copy(update={"passed": False}).model_dump(),
        "attempt": 1,
        "coverage_stall_count": 0,
        "deadline_epoch": time.time() - 1,
    }
    assert route_after_gate(state) == "failed"  # type: ignore[arg-type]


def test_route_after_gate_ignores_future_deadline(passing_review: ReviewOutcome) -> None:
    state = {
        "review_outcome": passing_review.model_copy(update={"passed": False}).model_dump(),
        "attempt": 1,
        "coverage_stall_count": 0,
        "deadline_epoch": time.time() + 3600,
    }
    assert route_after_gate(state) == "retry"  # type: ignore[arg-type]


def test_route_after_retry_fails_fast_on_deadline(passing_review: ReviewOutcome) -> None:
    state = {
        "retry_scope": "code",
        "review_outcome": passing_review.model_dump(),
        "deadline_epoch": time.time() - 1,
    }
    assert route_after_retry(state) == "failed"  # type: ignore[arg-type]


def test_route_after_gate_no_deadline_when_zero(passing_review: ReviewOutcome) -> None:
    state = {
        "review_outcome": passing_review.model_copy(update={"passed": False}).model_dump(),
        "attempt": 1,
        "coverage_stall_count": 0,
        "deadline_epoch": 0.0,
    }
    assert route_after_gate(state) == "retry"  # type: ignore[arg-type]


def test_route_after_retry_routes_by_scope(passing_review: ReviewOutcome) -> None:
    code_state = {
        "retry_scope": "code",
        "review_outcome": passing_review.model_dump(),
    }
    plan_state = {
        "retry_scope": "plan",
        "review_outcome": passing_review.model_dump(),
    }
    assert route_after_retry(code_state) == "code"  # type: ignore[arg-type]
    assert route_after_retry(plan_state) == "plan"  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_code_implement_stops_coder_before_work_lane(
    base_state: dict,
    task_plan: TaskPlan,
    code_change: CodeChange,
) -> None:
    """GX10: never load coder + Work lane simultaneously (unified memory)."""
    from sprint_crew.graph.pipeline import code_implement

    base_state["task_plan"] = task_plan.model_dump()
    lane_events: list[str] = []

    async def track_ensure(role: Role) -> None:
        lane_events.append(f"ensure:{role.value}")

    async def track_stop(role: Role) -> None:
        lane_events.append(f"stop:{role.value}")

    with (
        patch("sprint_crew.graph.pipeline.ensure_lane", side_effect=track_ensure),
        patch("sprint_crew.graph.pipeline.stop_lane", side_effect=track_stop),
        patch(
            "sprint_crew.graph.pipeline.run_coder_with_coverage",
            new=AsyncMock(
                return_value=(
                    "handoff",
                    [],
                    MagicMock(satisfied=True, missing=[], unexpected=[], out_of_scope_hits=[]),
                    "",
                    False,
                )
            ),
        ),
        patch("sprint_crew.graph.pipeline.gather_workspace_diff", return_value="diff"),
        patch("sprint_crew.graph.pipeline.run_formatter", new=AsyncMock(return_value=code_change)),
    ):
        await code_implement(base_state)  # type: ignore[arg-type]

    coder_stop_idx = lane_events.index("stop:coding")
    work_ensure_idx = lane_events.index("ensure:work")
    assert coder_stop_idx < work_ensure_idx, lane_events
