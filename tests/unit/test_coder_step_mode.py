from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from sprint_crew.schemas.ticket import PlanStep, TaskPlan


@pytest.fixture
def multi_step_plan() -> TaskPlan:
    return TaskPlan(
        ticket_key="DEMO-2",
        summary="multi file",
        steps=[
            PlanStep(description="create models", files=["models.py"]),
            PlanStep(description="create api", files=["api.py"]),
        ],
        files_to_touch=["models.py", "api.py"],
        acceptance_tests=["pytest -q"],
    )


@pytest.mark.asyncio
async def test_coder_plan_invokes_separate_loop_per_step_when_step_mode_on(
    multi_step_plan: TaskPlan,
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from sprint_crew.agents import coder

    monkeypatch.setenv("CODER_STEP_MODE", "true")
    from sprint_crew.config import get_settings

    get_settings.cache_clear()

    with patch.object(
        coder, "run_coder_loop", new=AsyncMock(return_value=("step done", []))
    ) as loop_mock:
        await coder.run_coder_plan(multi_step_plan, tmp_path)

    assert loop_mock.await_count == len(multi_step_plan.steps)
    get_settings.cache_clear()


@pytest.mark.asyncio
async def test_turn_budget_per_step_applies_lane_multiplier(
    multi_step_plan: TaskPlan,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from sprint_crew.agents import coder
    from sprint_crew.config import get_settings

    monkeypatch.setenv("MAX_CODER_TURNS", "32")
    get_settings.cache_clear()

    # 2 steps: effective total int(32*1.25)=40 → 20 per step (not raw 16).
    assert coder._turn_budget_per_step(multi_step_plan) == 20
    get_settings.cache_clear()


@pytest.mark.asyncio
async def test_coder_plan_single_loop_when_step_mode_off(
    multi_step_plan: TaskPlan,
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from sprint_crew.agents import coder

    monkeypatch.setenv("CODER_STEP_MODE", "false")
    from sprint_crew.config import get_settings

    get_settings.cache_clear()

    with patch.object(
        coder, "run_coder_loop", new=AsyncMock(return_value=("done", []))
    ) as loop_mock:
        await coder.run_coder_plan(multi_step_plan, tmp_path)

    loop_mock.assert_awaited_once()
    get_settings.cache_clear()
