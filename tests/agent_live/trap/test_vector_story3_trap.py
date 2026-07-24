from __future__ import annotations

import time
from uuid import uuid4

import pytest

from sprint_crew.orchestrator.session import create_and_run_cycle
from tests.helpers.agent_live_tickets import skip_template_fast_path, vector_story3_ticket
from tests.helpers.cycle_assertions import assert_cycle_passed
from tests.helpers.vector_ab import copy_fixture_workspace
from tests.helpers.vector_tiers import (
    collect_capability_metrics,
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
@pytest.mark.timeout(7200)
async def test_vector_story3_stdlib_shadow_trap(
    skip_unless_vllm_live,
    fixture_vector_repo_path,
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SCRUM-3 on base vector_repo — stdlib platform shadow trap (STRICT by default)."""
    skip_unless_vector_agent_live()
    if not docker_available():
        pytest.skip("docker not available")

    setup_vector_agent_env(monkeypatch)
    start_vector_stack()

    ticket = vector_story3_ticket()
    workspace = copy_fixture_workspace(
        fixture_vector_repo_path,
        tmp_path,
        name=f"vector-story3-trap-{uuid4().hex[:8]}",
    )
    session_id = f"vector-story3-trap-{uuid4().hex[:10]}"

    started = time.perf_counter()
    failure: str | None = None
    try:
        with skip_template_fast_path():
            session = await create_and_run_cycle(
                ticket=ticket,
                workspace=workspace,
                session_id=session_id,
            )
        duration_s = time.perf_counter() - started
        try:
            assert_cycle_passed(
                session,
                workspace=workspace,
                test_target="tests/test_notify_routes.py",
            )
        except AssertionError as exc:
            failure = str(exc)
        metrics = collect_capability_metrics(
            session,
            tier="trap",
            story_id="story3_stdlib_shadow",
            duration_s=duration_s,
        )
        report = {**metrics, "trap": "stdlib_platform_shadow", "failure": failure}
    except Exception as exc:
        duration_s = time.perf_counter() - started
        failure = str(exc)
        report = {
            "tier": "trap",
            "story_id": "story3_stdlib_shadow",
            "trap": "stdlib_platform_shadow",
            "duration_s": round(duration_s, 2),
            "failure": failure,
        }
        if trap_strict_mode():
            raise

    report_path = write_trap_report(report)
    print(f"trap story3 report: {report_path}")

    if failure is not None and trap_strict_mode():
        pytest.fail(failure)
    if failure is not None:
        print(f"trap SOFT pass-with-failure: {failure}")
