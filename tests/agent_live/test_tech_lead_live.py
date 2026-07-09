from __future__ import annotations

import pytest

from sprint_crew.agents.tech_lead import run_tech_lead
from sprint_crew.orchestrator.complexity import tech_lead_mode
from tests.helpers.agent_live_tickets import (
    complex_api_ticket,
    email_validators_ticket,
    skip_template_fast_path,
)
from tests.helpers.vllm_live import docker_available, wait_lane_healthy


@pytest.mark.agent_live
@pytest.mark.vllm_live
@pytest.mark.asyncio
@pytest.mark.timeout(600)
async def test_tech_lead_static_plan_validators(
    skip_unless_vllm_live,
    tmp_workspace,
) -> None:
    """Real work lane JSON — static mode for validators/email ticket (template bypassed)."""
    if not docker_available():
        pytest.skip("docker not available")

    from sprint_crew.config import get_settings

    ticket = email_validators_ticket()
    assert tech_lead_mode(ticket) == "static"

    settings = get_settings()
    wait_lane_healthy(settings.vllm_work_url.replace("/v1", "/health"))

    with skip_template_fast_path():
        plan, mode = await run_tech_lead(ticket, tmp_workspace)

    assert mode == "static"
    assert plan.ticket_key == "DEMO-2"
    assert "validators.py" in plan.files_to_touch


@pytest.mark.agent_live
@pytest.mark.vllm_live
@pytest.mark.asyncio
@pytest.mark.timeout(1800)
async def test_tech_lead_tool_loop_complex_ticket(
    skip_unless_vllm_live,
    tmp_workspace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Real work lane tool_loop — COMPLEX ticket explores repo then emits TaskPlan JSON."""
    if not docker_available():
        pytest.skip("docker not available")

    from sprint_crew.config import get_settings

    monkeypatch.setenv("MAX_TECHLEAD_TURNS", "24")
    get_settings.cache_clear()

    ticket = complex_api_ticket()
    assert tech_lead_mode(ticket) == "tool_loop"

    settings = get_settings()
    wait_lane_healthy(settings.vllm_work_url.replace("/v1", "/health"))

    with skip_template_fast_path():
        plan, mode = await run_tech_lead(ticket, tmp_workspace)

    assert mode == "tool_loop"
    assert plan.ticket_key == "DEMO-3"
    assert len(plan.files_to_touch) >= 1
    assert plan.acceptance_tests
