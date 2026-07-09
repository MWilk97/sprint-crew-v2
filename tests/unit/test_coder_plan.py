from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from sprint_crew.schemas.ticket import PlanStep, TaskPlan


@pytest.mark.asyncio
async def test_run_coder_plan_fast_path_single_step(tmp_path) -> None:
    from sprint_crew.agents import coder

    plan = TaskPlan(
        ticket_key="DEMO-1",
        summary="single",
        steps=[PlanStep(description="one", files=["a.py"])],
        acceptance_tests=["pytest -q"],
    )
    with patch.object(
        coder, "run_coder_loop", new=AsyncMock(return_value=("done", []))
    ) as loop_mock:
        await coder.run_coder_plan(plan, tmp_path)

    loop_mock.assert_awaited_once()
