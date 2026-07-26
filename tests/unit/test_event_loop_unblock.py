"""M3: agent model calls run via asyncio.to_thread, so a slow call does not freeze the loop.

This is the change that makes streaming believable — while a Reviewer/ScrumMaster call is in
flight, SSE frames and /health must still flush. A regression (dropping the to_thread wrap)
would re-block the loop and this test would see ~0 concurrent ticks.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from sprint_crew.agents import reviewer as rv
from sprint_crew.agents import scrum_master as sm
from sprint_crew.schemas.change import CodeChange, ReviewOutcome
from sprint_crew.schemas.ticket import TaskPlan


@pytest.mark.asyncio
async def test_blocking_model_call_does_not_freeze_event_loop() -> None:
    def _slow(*args: object, **kwargs: object) -> object:
        time.sleep(0.4)  # stand-in for a blocking OpenAI SDK call
        return object()

    ticks = 0

    async def _tick() -> None:
        nonlocal ticks
        while True:
            await asyncio.sleep(0.01)
            ticks += 1

    with (
        patch.object(sm, "structured_completion", _slow),
        patch.object(sm, "normalize_backlog_plan", return_value="ok"),
    ):
        ticker = asyncio.create_task(_tick())
        result = await sm.run_scrum_master(user_prompt="do it")
        ticker.cancel()

    assert result == "ok"
    # A frozen loop yields ~0 ticks across the 0.4s call; to_thread lets the ticker run.
    assert ticks > 10


@pytest.mark.asyncio
async def test_reviewer_acceptance_tests_do_not_freeze_event_loop(
    task_plan: TaskPlan, code_change: CodeChange, tmp_path: Path
) -> None:
    """run_acceptance_tests is a subprocess bounded by ACCEPTANCE_TEST_TIMEOUT_S (900 s).

    Unwrapped it stalls the process so hard that POST /cancel cannot even be accepted,
    breaking the cancel contract api/console.py documents. Reached whenever tests did not
    already pass this cycle — i.e. exactly the failing-test retry loop where Stop gets hit.
    """

    def _slow_tests(*args: object, **kwargs: object) -> tuple[str, bool]:
        time.sleep(0.4)  # stand-in for a pytest subprocess
        return ("collected 1 item", True)

    ticks = 0

    async def _tick() -> None:
        nonlocal ticks
        while True:
            await asyncio.sleep(0.01)
            ticks += 1

    review = ReviewOutcome(ticket_key="DEMO-1", passed=True, summary="ok", tests_passed=True)

    with (
        patch.object(rv, "run_acceptance_tests", _slow_tests),
        patch.object(rv, "gather_workspace_diff", return_value="diff"),
        patch.object(rv, "structured_completion", return_value=review),
    ):
        ticker = asyncio.create_task(_tick())
        outcome = await rv.run_reviewer(task_plan, code_change, tmp_path, tests_already_run=False)
        ticker.cancel()

    assert outcome.tests_passed is True
    assert ticks > 10
