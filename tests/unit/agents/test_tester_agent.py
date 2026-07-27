from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from sprint_crew.schemas.change import CodeChange, TestAdditions
from sprint_crew.schemas.ticket import PlanStep, TaskPlan
from sprint_crew.tools.pydantic_ai import build_tester_toolset


def test_tester_toolset_builds() -> None:
    assert build_tester_toolset() is not None


@pytest.mark.asyncio
async def test_tester_reporter_returns_test_additions_schema() -> None:
    from sprint_crew.agents.tester import run_tester_reporter

    plan = TaskPlan(
        ticket_key="DEMO-1",
        summary="Add tests",
        steps=[PlanStep(description="tests", files=["tests/test_greeter.py"])],
        acceptance_tests=["pytest -q"],
    )
    expected = TestAdditions(
        ticket_key="DEMO-1",
        tests_added=["tests/test_greeter.py"],
        coverage_summary="added hello test",
        tests_passed=True,
    )
    with patch(
        "sprint_crew.agents.tester.structured_completion",
        return_value=expected,
    ):
        result = await run_tester_reporter(plan, "handoff text")

    assert result.tests_passed is True
    assert result.ticket_key == "DEMO-1"


@pytest.mark.asyncio
async def test_run_tester_loop_records_tool_log(tmp_path: Path) -> None:
    from sprint_crew.agents import tester as tester_module
    from sprint_crew.agents.tester import run_tester_loop

    plan = TaskPlan(
        ticket_key="DEMO-1",
        summary="Add tests",
        steps=[PlanStep(description="tests", files=["tests/test_greeter.py"])],
        acceptance_tests=["pytest -q tests/test_greeter.py"],
    )
    change = CodeChange(
        ticket_key="DEMO-1",
        branch="feature/demo-1",
        summary="source change",
        tests_passed=False,
    )

    async def fake_run(_prompt, *, deps, **kwargs):
        from sprint_crew.tools.pydantic_ai import _record_tool_call

        args = {
            "path": "tests/test_extra.py",
            "content": "def test_extra():\n    assert True\n",
        }
        result = deps.registry.dispatch("write_file", args, workspace_root=deps.root)
        _record_tool_call(deps, "write_file", args, result.output, ok=result.ok)
        return MagicMock(output="Added tests under tests/")

    mock_agent = MagicMock()
    mock_agent.run = AsyncMock(side_effect=fake_run)

    with patch.object(tester_module, "build_tool_agent", return_value=mock_agent):
        _handoff, tool_log = await run_tester_loop(
            plan,
            change,
            tmp_path,
            acceptance_green=False,
            acceptance_output="failed",
        )

    assert any(entry.get("tool") == "write_file" for entry in tool_log)
    assert (tmp_path / "tests" / "test_extra.py").is_file()
