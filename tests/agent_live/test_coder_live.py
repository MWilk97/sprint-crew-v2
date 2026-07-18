from __future__ import annotations

import pytest

from sprint_crew.agents.coder import run_coder_loop
from sprint_crew.orchestrator.template_plan import build_template_task_plan_validated
from tests.helpers.agent_live_tickets import greeter_ticket
from tests.helpers.vllm_live import docker_available, wait_lane_healthy


@pytest.mark.agent_live
@pytest.mark.vllm_live
@pytest.mark.asyncio
@pytest.mark.timeout(600)
async def test_coder_tool_loop_writes_greeter(
    skip_unless_vllm_live,
    tmp_workspace,
) -> None:
    if not docker_available():
        pytest.skip("docker not available")

    from sprint_crew.config import get_settings

    settings = get_settings()
    wait_lane_healthy(settings.vllm_coder_url.replace("/v1", "/health"))

    ticket = greeter_ticket(
        summary="Add hello() to greeter.py",
        description="Implement hello() returning 'hello' and ensure pytest passes.",
        acceptance_criteria="pytest -q tests/test_greeter.py passes",
    )
    plan = build_template_task_plan_validated(ticket)

    _handoff, tool_log = await run_coder_loop(plan, tmp_workspace)

    assert any(entry.get("tool") in ("write_file", "apply_patch") for entry in tool_log)
    source = (tmp_workspace / "greeter.py").read_text(encoding="utf-8")
    assert "def hello" in source
