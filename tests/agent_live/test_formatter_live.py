from __future__ import annotations

import pytest

from sprint_crew.agents.formatter import run_formatter
from sprint_crew.config import get_settings
from sprint_crew.orchestrator.workspace_diff import gather_workspace_diff
from sprint_crew.schemas.ticket import PlanStep, TaskPlan
from tests.helpers.vllm_live import docker_available, wait_lane_healthy


@pytest.mark.agent_live
@pytest.mark.vllm_live
@pytest.mark.asyncio
@pytest.mark.timeout(600)
async def test_formatter_produces_code_change(
    skip_unless_vllm_live,
    tmp_workspace,
) -> None:
    """Real work lane JSON — Formatter normalizes branch/key from TaskPlan + git diff."""
    if not docker_available():
        pytest.skip("docker not available")

    settings = get_settings()
    wait_lane_healthy(settings.vllm_work_url.replace("/v1", "/health"))

    plan = TaskPlan(
        ticket_key="DEMO-1",
        summary="Add hello() to greeter.py",
        steps=[PlanStep(description="implement hello", files=["greeter.py"])],
        files_to_touch=["greeter.py"],
        acceptance_tests=["pytest -q tests/test_greeter.py"],
    )
    (tmp_workspace / "greeter.py").write_text(
        'def hello():\n    return "hello"\n',
        encoding="utf-8",
    )
    git_diff = gather_workspace_diff(tmp_workspace, priority_paths=["greeter.py"])
    handoff = (
        "Implemented hello() in greeter.py returning 'hello'. "
        "Ran pytest -q tests/test_greeter.py — tests passed."
    )

    result = await run_formatter(
        task_plan=plan,
        raw_output=handoff,
        git_diff=git_diff,
    )

    assert result.ticket_key == "DEMO-1"
    assert result.branch == "feature/demo-1"
    assert result.summary.strip()
