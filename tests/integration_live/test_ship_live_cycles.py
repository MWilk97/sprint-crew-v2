from __future__ import annotations

import pytest

from sprint_crew.config import Settings
from tests.helpers.ship_live_cycle import run_fixture_ship_live_cycle


@pytest.mark.integration_live
@pytest.mark.vllm_live
@pytest.mark.asyncio
@pytest.mark.timeout(2400)
async def test_greeter_full_cycle_real_ship(integration_vllm_env: Settings) -> None:
    await run_fixture_ship_live_cycle(
        integration_vllm_env,
        fixture_rel=integration_vllm_env.project_root / "fixtures" / "repo",
        summary_suffix="greeter ship live",
        description="Implement hello() returning 'hello' and ensure pytest passes.",
        acceptance_criteria="pytest -q tests/test_greeter.py passes",
    )


@pytest.mark.integration_live
@pytest.mark.vllm_live
@pytest.mark.asyncio
@pytest.mark.timeout(2400)
async def test_email_validation_full_cycle_real_ship(integration_vllm_env: Settings) -> None:
    await run_fixture_ship_live_cycle(
        integration_vllm_env,
        fixture_rel=integration_vllm_env.project_root / "fixtures" / "repo",
        summary_suffix="email ship live",
        description=(
            "Implement validate_email(address) in validators.py returning True for simple "
            "addresses with @ and a domain, False otherwise. Ensure pytest passes."
        ),
        acceptance_criteria="pytest -q tests/test_validators.py passes",
    )
