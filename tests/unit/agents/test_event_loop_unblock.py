"""M3: agent model calls run via asyncio.to_thread, so a slow call does not freeze the loop.

This is the change that makes streaming believable — while a Reviewer/ScrumMaster call is in
flight, SSE frames and /health must still flush. A regression (dropping the to_thread wrap)
would re-block the loop and this test would see ~0 concurrent ticks.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from types import SimpleNamespace
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
    assert ticks > 3


@pytest.mark.asyncio
async def test_reviewer_acceptance_tests_do_not_freeze_event_loop(
    task_plan: TaskPlan, code_change: CodeChange, tmp_path: Path
) -> None:
    """run_acceptance_tests is a subprocess bounded by ACCEPTANCE_TEST_TIMEOUT_S (900 s).

    Run on the loop it stalls the process so hard that POST /cancel cannot even be
    accepted, breaking the cancel contract api/console.py documents. Reached whenever tests
    did not already pass this cycle — i.e. exactly the failing-test retry loop where Stop
    gets hit.

    Deliberately drives the real subprocess path rather than a stub: the guarantee now
    lives in sprint_crew.proc, so mocking run_acceptance_tests would assert nothing.

    The sleep goes through `python -c` because acceptance commands are validated against
    the allowlist at run time now, and `sleep` is not on it.
    """
    task_plan = task_plan.model_copy(
        update={"acceptance_tests": ['python -c "import time; time.sleep(0.4)"']}
    )

    ticks = 0

    async def _tick() -> None:
        nonlocal ticks
        while True:
            await asyncio.sleep(0.01)
            ticks += 1

    review = ReviewOutcome(ticket_key="DEMO-1", passed=True, summary="ok", tests_passed=True)

    with (
        patch.object(rv, "gather_workspace_diff", return_value="diff"),
        patch.object(rv, "structured_completion", return_value=review),
    ):
        ticker = asyncio.create_task(_tick())
        outcome = await rv.run_reviewer(
            task_plan, code_change, tmp_path, prior_tests_verified=False
        )
        ticker.cancel()

    assert outcome.tests_passed is True
    assert ticks > 3


@pytest.mark.asyncio
async def test_tool_dispatch_does_not_freeze_event_loop(tmp_path: Path) -> None:
    """Tool bodies are blocking — run_command spawns a child with a 300 s cap, the git
    tools and grep spawn subprocesses. The closures are async, so dispatching on the loop
    froze it for the whole tool call on *every* coder and tester turn, not once per cycle.

    Patches the tool's own `execute`, not the registry: dispatch goes through `adispatch`,
    which is the seam that decides between a worker thread and a native async body.
    """
    from sprint_crew.tools.base import ToolResult
    from sprint_crew.tools.pydantic_ai import build_coder_toolset, workspace_deps

    deps = workspace_deps(tmp_path, mutate=True, event_agent="coder")

    def _slow_execute(*args: object, **kwargs: object) -> ToolResult:
        time.sleep(0.4)  # stand-in for a subprocess-backed tool
        return ToolResult(ok=True, output="done")

    ticks = 0

    async def _tick() -> None:
        nonlocal ticks
        while True:
            await asyncio.sleep(0.01)
            ticks += 1

    toolset = build_coder_toolset()
    grep_tool = toolset.tools["grep"]

    with patch.object(deps.registry.get("grep"), "execute", _slow_execute):
        ticker = asyncio.create_task(_tick())
        ctx = SimpleNamespace(deps=deps)
        out = await grep_tool.function(ctx, pattern="x", path=".")
        ticker.cancel()

    assert out == "done"
    assert ticks > 3
