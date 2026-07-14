from __future__ import annotations

import time

import pytest

from tests.helpers.agent_live_tickets import skip_template_fast_path
from tests.helpers.from_prompt_assertions import evaluate_from_prompt_run
from tests.helpers.from_prompt_live import (
    VECTOR_INTEGRATION_PROMPT,
    run_from_prompt_live,
    write_from_prompt_integration_report,
)
from tests.helpers.vector_tiers import (
    setup_vector_agent_env,
    skip_unless_vector_agent_live,
    start_vector_stack,
)
from tests.helpers.vllm_live import docker_available


@pytest.mark.agent_live
@pytest.mark.vllm_live
@pytest.mark.vector_agent_live
@pytest.mark.agent_integration
@pytest.mark.nightly
@pytest.mark.asyncio
@pytest.mark.timeout(10800)
async def test_from_prompt_vector_2story_integration(
    skip_unless_vllm_live,
    fixture_vector_repo_path,
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Nightly HARD gate: from-prompt backlog with exactly 2 stories (queue + retry)."""
    skip_unless_vector_agent_live()
    if not docker_available():
        pytest.skip("docker not available")

    setup_vector_agent_env(monkeypatch)
    start_vector_stack()

    started = time.perf_counter()
    check = None
    try:
        with skip_template_fast_path():
            result = await run_from_prompt_live(
                prompt=VECTOR_INTEGRATION_PROMPT,
                fixture_path=fixture_vector_repo_path,
                tmp_path=tmp_path,
                use_real_ship=False,
            )
        duration_s = time.perf_counter() - started
        check = evaluate_from_prompt_run(
            result,
            duration_s,
            tier="integration",
            story_count_exact=2,
            post_check_fragments=("ferry", "retry"),
        )
    except Exception:
        duration_s = time.perf_counter() - started
        raise
    finally:
        if check is not None:
            report_path = write_from_prompt_integration_report(check.to_report_payload())
            print(f"from-prompt integration report: {report_path}")

    if check.failure is not None:
        pytest.fail(check.failure)
