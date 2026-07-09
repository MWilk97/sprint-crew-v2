from __future__ import annotations

from contextlib import asynccontextmanager
from unittest.mock import MagicMock, patch

import pytest

from sprint_crew.schemas.ticket import PlanStep, TaskPlan


@pytest.mark.asyncio
async def test_run_coder_loop_handles_none_result(tmp_path) -> None:
    from sprint_crew.agents import coder

    plan = TaskPlan(
        ticket_key="DEMO-1",
        summary="edit",
        steps=[PlanStep(description="edit", files=["a.py"])],
        acceptance_tests=["pytest -q"],
    )

    @asynccontextmanager
    async def fake_iter(*_args, **_kwargs):
        run = MagicMock()
        run.result = None

        async def empty_iter():
            if False:
                yield None

        run.__aiter__ = lambda self: empty_iter()
        yield run

    agent_mock = MagicMock()
    agent_mock.iter = fake_iter

    with patch.object(
        coder, "_build_coder_agent", return_value=(agent_mock, MagicMock(early_exit_handoff=None))
    ):
        handoff, tool_log = await coder.run_coder_loop(plan, tmp_path)

    assert handoff == "Coder loop ended without structured handoff."
    assert tool_log == []


@pytest.mark.asyncio
async def test_run_coder_loop_handles_model_http_error(tmp_path) -> None:
    """A mid-loop HTTP 400 (context overflow) hands off partial work, not a crash."""
    from pydantic_ai.exceptions import ModelHTTPError

    from sprint_crew.agents import coder

    plan = TaskPlan(
        ticket_key="DEMO-1",
        summary="edit",
        steps=[PlanStep(description="edit", files=["a.py"])],
        acceptance_tests=["pytest -q"],
    )

    @asynccontextmanager
    async def fake_iter(*_args, **_kwargs):
        run = MagicMock()
        run.result = None

        async def raising_iter():
            raise ModelHTTPError(
                status_code=400, model_name="qwen3-coder-next", body={"message": "too long"}
            )
            yield None  # pragma: no cover

        run.__aiter__ = lambda self: raising_iter()
        yield run

    agent_mock = MagicMock()
    agent_mock.iter = fake_iter

    with patch.object(
        coder, "_build_coder_agent", return_value=(agent_mock, MagicMock(early_exit_handoff=None))
    ):
        handoff, tool_log = await coder.run_coder_loop(plan, tmp_path)

    assert "handing off partial work" in handoff
    assert "400" in handoff
