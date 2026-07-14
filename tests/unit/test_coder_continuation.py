from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from pydantic_ai.exceptions import UsageLimitExceeded

from sprint_crew.orchestrator.plan_coverage import PlanCoverageResult
from sprint_crew.schemas.ticket import PlanStep, TaskPlan


@pytest.fixture
def plan_with_missing_source() -> TaskPlan:
    return TaskPlan(
        ticket_key="DEMO-1",
        summary="add feature",
        steps=[PlanStep(description="create module", files=["feature.py"])],
        files_to_touch=["feature.py"],
        acceptance_tests=["pytest -q"],
    )


@pytest.mark.asyncio
async def test_run_coder_with_coverage_skips_unfixable_continuation(
    plan_with_missing_source: TaskPlan,
    tmp_path,
) -> None:
    from sprint_crew.agents import coder

    unsatisfied = PlanCoverageResult(
        missing=["tests/test_feature.py"],
        unexpected=[],
        out_of_scope_hits=[],
        satisfied=False,
    )
    with (
        patch.object(coder, "run_coder_plan", new=AsyncMock(return_value=("done", []))),
        patch.object(coder, "validate_plan_coverage", return_value=unsatisfied),
        patch.object(coder, "continuation_makes_sense", return_value=False),
        patch.object(coder, "run_coder_loop", new=AsyncMock()) as loop_mock,
    ):
        _, _, coverage, _, _ = await coder.run_coder_with_coverage(
            plan_with_missing_source, tmp_path
        )

    loop_mock.assert_not_awaited()
    assert coverage.satisfied is False


@pytest.mark.asyncio
async def test_run_coder_with_coverage_stops_on_stall(
    plan_with_missing_source: TaskPlan, tmp_path
) -> None:
    from sprint_crew.agents import coder

    stalled = PlanCoverageResult(
        missing=["feature.py"],
        unexpected=[],
        out_of_scope_hits=[],
        satisfied=False,
    )

    with (
        patch.object(coder, "run_coder_plan", new=AsyncMock(return_value=("done", []))),
        patch.object(coder, "validate_plan_coverage", side_effect=[stalled, stalled]),
        patch.object(coder, "continuation_makes_sense", return_value=True),
        patch.object(
            coder, "run_coder_loop", new=AsyncMock(return_value=("still missing", []))
        ) as loop_mock,
    ):
        _, _, coverage, _, _ = await coder.run_coder_with_coverage(
            plan_with_missing_source, tmp_path
        )

    loop_mock.assert_awaited_once()
    assert coverage.satisfied is False


@pytest.mark.asyncio
async def test_run_coder_loop_usage_limit_returns_partial(tmp_path) -> None:
    from sprint_crew.agents import coder

    plan = TaskPlan(
        ticket_key="DEMO-1",
        summary="single",
        steps=[PlanStep(description="one", files=["a.py"])],
        acceptance_tests=["pytest -q"],
    )

    class FakeIter:
        async def __aenter__(self):
            raise UsageLimitExceeded("limit")

        async def __aexit__(self, *args):
            return False

    class FakeAgent:
        def iter(self, *args, **kwargs):
            return FakeIter()

    agent, deps = coder._build_coder_agent(tmp_path, plan)
    with patch.object(coder, "_build_coder_agent", return_value=(FakeAgent(), deps)):
        output, log = await coder.run_coder_loop(plan, tmp_path, max_turns=3)

    assert "exhausted" in output.lower()
    assert log == []
