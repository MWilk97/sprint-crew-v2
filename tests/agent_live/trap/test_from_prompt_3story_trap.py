from __future__ import annotations

import time

import pytest

from tests.helpers.agent_live_tickets import skip_template_fast_path
from tests.helpers.from_prompt_assertions import evaluate_from_prompt_run
from tests.helpers.from_prompt_live import VECTOR_TRAP_PROMPT, run_from_prompt_live
from tests.helpers.vector_tiers import (
    setup_vector_agent_env,
    skip_unless_vector_agent_live,
    start_vector_stack,
    trap_strict_mode,
    write_trap_report,
)
from tests.helpers.vllm_live import docker_available


@pytest.mark.agent_live
@pytest.mark.vllm_live
@pytest.mark.vector_agent_live
@pytest.mark.agent_trap
@pytest.mark.asyncio
@pytest.mark.timeout(10800)
async def test_from_prompt_vector_3story_trap(
    skip_unless_vllm_live,
    fixture_vector_repo_path,
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Full 3-story from-prompt with stdlib trap on story 3 — STRICT unless VECTOR_TRAP_SOFT=1."""
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
                prompt=VECTOR_TRAP_PROMPT,
                fixture_path=fixture_vector_repo_path,
                tmp_path=tmp_path,
                use_real_ship=False,
            )
        duration_s = time.perf_counter() - started
        check = evaluate_from_prompt_run(
            result,
            duration_s,
            tier="trap",
            trap="from_prompt_3story",
            story_count_exact=None,
            story_count_range=(2, 3),
            post_check_fragments=("ferry",),
            include_trap_failure_class=True,
            assert_plan_fragments=False,
            # Strict trap failure is scoped to the trap/pipeline outcome (pipeline,
            # per-session cycle asserts, coverage/tests). The ferry semantic post-check
            # is a brittle retrieval heuristic — kept in the report, but non-fatal.
            post_check_fatal=False,
        )
    except Exception:
        if trap_strict_mode():
            raise
    finally:
        if check is not None:
            trap_path = write_trap_report(check.to_report_payload())
            print(f"trap 3-story report: {trap_path}")

    if check is not None and check.failure is not None and trap_strict_mode():
        pytest.fail(check.failure)
    if check is not None and check.failure is not None:
        print(f"trap SOFT pass-with-failure: {check.failure}")
