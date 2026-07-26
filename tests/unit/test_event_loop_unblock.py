"""M3: agent model calls run via asyncio.to_thread, so a slow call does not freeze the loop.

This is the change that makes streaming believable — while a Reviewer/ScrumMaster call is in
flight, SSE frames and /health must still flush. A regression (dropping the to_thread wrap)
would re-block the loop and this test would see ~0 concurrent ticks.
"""

from __future__ import annotations

import asyncio
import time
from unittest.mock import patch

import pytest

from sprint_crew.agents import scrum_master as sm


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
