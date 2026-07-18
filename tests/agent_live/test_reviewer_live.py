from __future__ import annotations

import pytest

from sprint_crew.agents.reviewer import run_reviewer
from sprint_crew.config import get_settings
from tests.helpers.agent_live_tickets import greeter_code_change, greeter_task_plan
from tests.helpers.vllm_live import docker_available, wait_lane_healthy


@pytest.mark.agent_live
@pytest.mark.vllm_live
@pytest.mark.asyncio
@pytest.mark.timeout(600)
async def test_reviewer_accepts_green_fixture(
    skip_unless_vllm_live,
    tmp_workspace,
) -> None:
    if not docker_available():
        pytest.skip("docker not available")

    settings = get_settings()
    wait_lane_healthy(settings.vllm_work_url.replace("/v1", "/health"))

    plan = greeter_task_plan()
    change = greeter_code_change()
    (tmp_workspace / "greeter.py").write_text(
        'def hello():\n    return "hello"\n',
        encoding="utf-8",
    )

    result = await run_reviewer(
        plan,
        change,
        tmp_workspace,
        ticket_acceptance_criteria="- hello() returns 'hello'",
    )

    assert result.tests_passed is True
    assert result.passed is True
    assert result.ticket_key == "DEMO-1"


@pytest.mark.agent_live
@pytest.mark.vllm_live
@pytest.mark.asyncio
@pytest.mark.timeout(600)
async def test_reviewer_rejects_red_fixture(
    skip_unless_vllm_live,
    tmp_workspace,
) -> None:
    """Broken greeter fixture — orchestrator pytest red overrides optimistic LLM JSON."""
    if not docker_available():
        pytest.skip("docker not available")

    settings = get_settings()
    wait_lane_healthy(settings.vllm_work_url.replace("/v1", "/health"))

    plan = greeter_task_plan()
    change = greeter_code_change(tests_passed=False, summary="incomplete greeter change")

    result = await run_reviewer(
        plan,
        change,
        tmp_workspace,
        ticket_acceptance_criteria="- hello() returns 'hello'",
    )

    assert result.tests_passed is False
    assert result.passed is False
    assert result.ticket_key == "DEMO-1"
