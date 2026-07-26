from __future__ import annotations

import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from tests.helpers.acceptance_output import SCRUM3_COLLECTION
from tests.helpers.ticket_fixtures import greeter_ticket

from sprint_crew.config import Role
from sprint_crew.graph.pipeline import (
    build_sprint_graph,
    code_implement,
    prepare_retry,
    route_after_gate,
    route_after_plan,
    route_after_retry,
)

# Aliased: pytest would collect the pipeline node as a test case under its own name.
from sprint_crew.graph.pipeline import test_implement as run_test_implement
from sprint_crew.schemas.change import CodeChange, ReviewOutcome
from sprint_crew.schemas.session import SessionStatus
from sprint_crew.schemas.ticket import PlanStep, TaskPlan


@pytest.fixture
def graph_state(base_state: dict) -> dict:
    graph_state = dict(base_state)
    graph_state["session_id"] = "graph-retry"
    return graph_state


@pytest.fixture
def base_retry_state(tmp_path) -> dict:
    plan = TaskPlan(
        ticket_key="DEMO-1",
        summary="Add hello()",
        steps=[PlanStep(description="edit greeter", files=["greeter.py"])],
        acceptance_tests=["pytest -q"],
    )
    return {
        "session_id": "retry-session",
        "workspace_root": str(tmp_path),
        "selected_ticket": {
            "key": "DEMO-1",
            "summary": "Add hello()",
            "status": "To Do",
            "issue_type": "Story",
        },
        "task_plan": plan.model_dump(),
        "attempt": 0,
        "plan_retries": 0,
        "workspace_diff": "diff snippet",
        "events": [],
    }


def _failing_review() -> ReviewOutcome:
    return ReviewOutcome(
        ticket_key="DEMO-1",
        passed=False,
        summary="fix hello()",
        tests_passed=False,
        retry_scope="code",
    )


@pytest.mark.asyncio
async def test_graph_happy_path_mocked(
    base_state: dict,
    task_plan: TaskPlan,
    code_change: CodeChange,
    passing_review: ReviewOutcome,
    graph_run_mocks,
) -> None:
    graph = build_sprint_graph()

    with graph_run_mocks(
        tech_lead_result=(task_plan, "static", []),
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
    """All tickets enter techLeadPlan; mode selection is internal."""
    graph = build_sprint_graph()
    node_names = set(graph.nodes.keys())
    assert "techLeadPlan" in node_names
    assert "templateTechLead" not in node_names
    assert "skipTechLead" not in node_names


@pytest.mark.asyncio
async def test_graph_simple_ticket_uses_template_via_tech_lead(
    base_state: dict,
    code_change: CodeChange,
    passing_review: ReviewOutcome,
    graph_run_mocks,
) -> None:
    base_state["selected_ticket"] = greeter_ticket().model_dump()
    graph = build_sprint_graph()

    with graph_run_mocks(
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


@pytest.mark.asyncio
async def test_test_implement_skips_acceptance_when_coder_verified(
    base_state: dict,
    task_plan: TaskPlan,
    code_change: CodeChange,
) -> None:
    base_state["task_plan"] = task_plan.model_dump()
    base_state["code_change"] = code_change.model_dump()
    base_state["tests_run_this_cycle"] = True
    base_state["acceptance_test_output"] = "pytest ok"

    with patch(
        "sprint_crew.orchestrator.acceptance_tests.run_acceptance_tests",
    ) as run_ac:
        await run_test_implement(base_state)  # type: ignore[arg-type]

    run_ac.assert_not_called()


@pytest.mark.asyncio
async def test_test_implement_keeps_work_lane_warm_after_reporter(
    base_state: dict,
    task_plan: TaskPlan,
    code_change: CodeChange,
) -> None:
    base_state["task_plan"] = task_plan.model_dump()
    base_state["code_change"] = code_change.model_dump()
    lane_events: list[str] = []

    async def track_stop(role: Role) -> None:
        lane_events.append(f"stop:{role.value}")

    with (
        patch("sprint_crew.graph.lanes.stop_lane", side_effect=track_stop),
        patch("sprint_crew.graph.lanes.ensure_lane", new=AsyncMock()),
        patch(
            "sprint_crew.agents.tester.run_tester_loop",
            new=AsyncMock(return_value=("handoff", [])),
        ),
        patch("sprint_crew.orchestrator.workspace_diff.gather_workspace_diff", return_value="diff"),
        patch("sprint_crew.agents.tester.run_tester_reporter", new=AsyncMock(return_value=None)),
        patch(
            "sprint_crew.orchestrator.acceptance_tests.run_acceptance_tests",
            return_value=("", False),
        ),
        patch("sprint_crew.orchestrator.plan_coverage.should_invoke_tester", return_value=True),
    ):
        await run_test_implement(base_state)  # type: ignore[arg-type]

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


@pytest.mark.asyncio
async def test_deadline_epoch_survives_graph_input_roundtrip() -> None:
    """deadline_epoch must be a declared SprintState channel or LangGraph's input
    mapping drops it, leaving _deadline_exceeded permanently False. The route_* tests
    above pass hand-built dicts straight to the routing functions and so cannot catch
    the drop — this exercises a real graph input round-trip.
    """
    from langgraph.graph import END, START, StateGraph

    from sprint_crew.graph.pipeline import _deadline_exceeded
    from sprint_crew.graph.state import SprintState

    seen: dict = {}

    def capture(state: SprintState) -> dict:
        seen.update(state)
        return {}

    graph: StateGraph = StateGraph(SprintState)
    graph.add_node("capture", capture)
    graph.add_edge(START, "capture")
    graph.add_edge("capture", END)
    app = graph.compile()

    await app.ainvoke(
        {
            "session_id": "deadline-roundtrip",
            "workspace_root": "/tmp/does-not-matter",
            "status": SessionStatus.RUNNING,
            "events": [],
            "deadline_epoch": time.time() - 60,
        }
    )

    assert "deadline_epoch" in seen
    assert _deadline_exceeded(seen) is True  # type: ignore[arg-type]


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
    satisfied_coder_result: tuple,
) -> None:
    """GX10: never load coder + Work lane simultaneously (unified memory)."""
    base_state["task_plan"] = task_plan.model_dump()
    lane_events: list[str] = []

    async def track_ensure(role: Role) -> None:
        lane_events.append(f"ensure:{role.value}")

    async def track_stop(role: Role) -> None:
        lane_events.append(f"stop:{role.value}")

    with (
        patch("sprint_crew.graph.lanes.ensure_lane", side_effect=track_ensure),
        patch("sprint_crew.graph.lanes.stop_lane", side_effect=track_stop),
        patch(
            "sprint_crew.agents.coder_coverage.run_coder_with_coverage",
            new=AsyncMock(return_value=satisfied_coder_result),
        ),
        patch("sprint_crew.orchestrator.workspace_diff.gather_workspace_diff", return_value="diff"),
        patch(
            "sprint_crew.agents.formatter.run_formatter", new=AsyncMock(return_value=code_change)
        ),
    ):
        await code_implement(base_state)  # type: ignore[arg-type]

    coder_stop_idx = lane_events.index("stop:coding")
    work_ensure_idx = lane_events.index("ensure:work")
    assert coder_stop_idx < work_ensure_idx, lane_events


@pytest.mark.asyncio
async def test_graph_review_fail_then_code_retry_reaches_ship(
    graph_state: dict,
    task_plan: TaskPlan,
    code_change: CodeChange,
    passing_review: ReviewOutcome,
    graph_run_mocks,
) -> None:
    graph = build_sprint_graph()
    reviews = [_failing_review(), passing_review]

    with graph_run_mocks(
        tech_lead_result=(task_plan, "template", []),
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
    passing_review: ReviewOutcome,
    graph_run_mocks,
) -> None:
    graph = build_sprint_graph()
    plan_review = _failing_review().model_copy(
        update={"retry_scope": "plan", "summary": "wrong files"}
    )

    with graph_run_mocks(
        tech_lead_result=(task_plan, "static", []),
        formatter_result=code_change,
        should_invoke_tester=False,
        reviewer=AsyncMock(side_effect=[plan_review, passing_review]),
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
    graph_state["attempt"] = 4
    graph = build_sprint_graph()

    with graph_run_mocks(
        tech_lead_result=(task_plan, "template", []),
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
    passing_review: ReviewOutcome,
    graph_run_mocks,
) -> None:
    graph = build_sprint_graph()
    unsatisfied = MagicMock(
        satisfied=False, missing=["greeter.py"], unexpected=[], out_of_scope_hits=[]
    )

    with graph_run_mocks(
        tech_lead_result=(task_plan, "template", []),
        coder_result=("handoff", [], unsatisfied, "", False),
        formatter_result=code_change,
        should_invoke_tester=False,
        reviewer=AsyncMock(return_value=passing_review),
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


@pytest.mark.asyncio
async def test_test_implement_records_acceptance_failure_when_collection_fails(
    base_state: dict,
    task_plan: TaskPlan,
    code_change: CodeChange,
) -> None:
    base_state["task_plan"] = task_plan.model_dump()
    base_state["code_change"] = code_change.model_dump()

    with (
        patch(
            "sprint_crew.orchestrator.acceptance_tests.run_acceptance_tests",
            return_value=(SCRUM3_COLLECTION, False),
        ),
        patch("sprint_crew.orchestrator.plan_coverage.should_invoke_tester", return_value=False),
    ):
        result = await run_test_implement(base_state)  # type: ignore[arg-type]

    assert result["acceptance_failure"]
    assert result["acceptance_failure"]["kind"] != "none"


@pytest.mark.asyncio
async def test_test_implement_clears_stale_acceptance_failure_once_tests_are_green(
    base_state: dict,
    task_plan: TaskPlan,
    code_change: CodeChange,
) -> None:
    """Regression: a failure recorded in an earlier retry round must not leak into
    a later round's feedback once tests are green again — acceptance_failure has no
    LangGraph reducer, so the node must explicitly clear it every round."""
    base_state["task_plan"] = task_plan.model_dump()
    base_state["code_change"] = code_change.model_dump()
    base_state["acceptance_failure"] = {
        "kind": "collection_error",
        "tester_can_help": False,
        "source_paths": ["src/api/routes.py"],
        "test_paths": [],
        "summary": "stale failure from a prior round",
        "detail_excerpt": "",
    }

    with (
        patch(
            "sprint_crew.orchestrator.acceptance_tests.run_acceptance_tests",
            return_value=("exit_code=0", True),
        ),
        patch("sprint_crew.orchestrator.plan_coverage.should_invoke_tester", return_value=False),
    ):
        result = await run_test_implement(base_state)  # type: ignore[arg-type]

    assert result["acceptance_failure"] == {}


@pytest.mark.asyncio
async def test_prepare_retry_increments_attempt(base_retry_state: dict) -> None:
    base_retry_state["review_outcome"] = ReviewOutcome(
        ticket_key="DEMO-1",
        passed=False,
        summary="fix it",
        tests_passed=False,
        retry_scope="code",
    ).model_dump()

    with patch(
        "sprint_crew.orchestrator.acceptance_tests.run_acceptance_tests",
        return_value=("stderr", False),
    ):
        result = await prepare_retry(base_retry_state)  # type: ignore[arg-type]

    assert result["attempt"] == 1
    assert result["prior_review_feedback"]


@pytest.mark.asyncio
async def test_prepare_retry_code_scope_skips_tester_when_tests_green(
    base_retry_state: dict,
) -> None:
    base_retry_state["review_outcome"] = ReviewOutcome(
        ticket_key="DEMO-1",
        passed=False,
        summary="style issue",
        tests_passed=True,
        retry_scope="code",
    ).model_dump()
    base_retry_state["tests_run_this_cycle"] = True
    base_retry_state["code_change"] = CodeChange(
        ticket_key="DEMO-1",
        branch="feature/demo-1",
        summary="done",
        tests_passed=True,
    ).model_dump()

    with patch("sprint_crew.orchestrator.acceptance_tests.run_acceptance_tests") as run_tests:
        result = await prepare_retry(base_retry_state)  # type: ignore[arg-type]

    run_tests.assert_not_called()
    assert result["skip_tester_this_attempt"] is True


@pytest.mark.asyncio
async def test_prepare_retry_plan_scope_increments_plan_retries(base_retry_state: dict) -> None:
    base_retry_state["review_outcome"] = ReviewOutcome(
        ticket_key="DEMO-1",
        passed=False,
        summary="wrong plan",
        tests_passed=False,
        retry_scope="plan",
    ).model_dump()

    with patch(
        "sprint_crew.orchestrator.acceptance_tests.run_acceptance_tests", return_value=("", False)
    ):
        result = await prepare_retry(base_retry_state)  # type: ignore[arg-type]

    assert result["plan_retries"] == 1


@pytest.mark.asyncio
async def test_prepare_retry_wires_test_output_into_feedback(base_retry_state: dict) -> None:
    """format_review_feedback's own content rendering is covered by
    test_retry_and_acceptance.py; this only proves prepare_retry wires the workspace
    diff and rerun test output through to the feedback at all."""
    base_retry_state["review_outcome"] = ReviewOutcome(
        ticket_key="DEMO-1",
        passed=False,
        summary="tests red",
        tests_passed=False,
        retry_scope="code",
    ).model_dump()

    with patch(
        "sprint_crew.orchestrator.acceptance_tests.run_acceptance_tests",
        return_value=("AssertionError: boom", False),
    ):
        result = await prepare_retry(base_retry_state)  # type: ignore[arg-type]

    assert result["prior_review_feedback"]
    assert result["retry_scope"] == "code"
    assert result["attempt"] == 1
