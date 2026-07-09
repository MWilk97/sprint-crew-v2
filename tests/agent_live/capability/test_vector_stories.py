from __future__ import annotations

import time
from pathlib import Path
from uuid import uuid4

import pytest

from sprint_crew.orchestrator.session import create_and_run_cycle
from tests.helpers.agent_live_tickets import (
    skip_template_fast_path,
    vector_story1_ticket,
    vector_story2_ticket,
    vector_story3_ticket,
)
from tests.helpers.vector_ab import copy_fixture_workspace
from tests.helpers.vector_fixtures import copy_vector_fixture
from tests.helpers.vector_tiers import (
    assert_capability_cycle,
    collect_capability_metrics,
    setup_vector_agent_env,
    skip_unless_vector_agent_live,
    start_vector_stack,
    write_capability_report,
)
from tests.helpers.vllm_live import docker_available


async def _run_capability_cycle(
    *,
    ticket,
    tmp_path,
    story_id: str,
    test_target: str,
    workspace_name: str,
    fixture_path: Path | None = None,
    overlay: str | None = None,
) -> dict:
    ws_name = f"{workspace_name}-{uuid4().hex[:8]}"
    if overlay:
        workspace = copy_vector_fixture(tmp_path, overlay=overlay, name=ws_name)
    else:
        workspace = copy_fixture_workspace(
            fixture_path,  # type: ignore[arg-type]
            tmp_path,
            name=ws_name,
        )
    session_id = f"{workspace_name}-{uuid4().hex[:10]}"
    started = time.perf_counter()
    with skip_template_fast_path():
        session = await create_and_run_cycle(
            ticket=ticket,
            workspace=workspace,
            session_id=session_id,
        )
    duration_s = time.perf_counter() - started
    assert_capability_cycle(session, workspace=workspace, test_target=test_target)
    metrics = collect_capability_metrics(
        session,
        tier="capability",
        story_id=story_id,
        duration_s=duration_s,
    )
    metrics["test_target"] = test_target
    return metrics


@pytest.mark.agent_live
@pytest.mark.vllm_live
@pytest.mark.vector_agent_live
@pytest.mark.agent_capability
@pytest.mark.asyncio
@pytest.mark.timeout(7200)
async def test_vector_story1_queue_cycle(
    skip_unless_vllm_live,
    fixture_vector_repo_path,
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Story 1 — persistent outbound queue + ferry dispatch (vector_repo)."""
    skip_unless_vector_agent_live()
    if not docker_available():
        pytest.skip("docker not available")

    setup_vector_agent_env(monkeypatch)
    start_vector_stack()

    metrics = await _run_capability_cycle(
        ticket=vector_story1_ticket(),
        fixture_path=fixture_vector_repo_path,
        tmp_path=tmp_path,
        story_id="story1_queue",
        test_target="tests/test_ferry_queue.py",
        workspace_name="vector-story1",
    )
    report_path = write_capability_report(metrics)
    print(f"capability story1 report: {report_path}")


@pytest.mark.agent_live
@pytest.mark.vllm_live
@pytest.mark.vector_agent_live
@pytest.mark.agent_capability
@pytest.mark.asyncio
@pytest.mark.timeout(7200)
async def test_vector_story2_retry_cycle(
    skip_unless_vllm_live,
    fixture_vector_repo_path,
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Story 2 — exponential backoff retry policy (vector_repo)."""
    skip_unless_vector_agent_live()
    if not docker_available():
        pytest.skip("docker not available")

    setup_vector_agent_env(monkeypatch)
    start_vector_stack()

    metrics = await _run_capability_cycle(
        ticket=vector_story2_ticket(),
        fixture_path=fixture_vector_repo_path,
        tmp_path=tmp_path,
        story_id="story2_retry",
        test_target="tests/test_ferry_retry.py",
        workspace_name="vector-story2",
    )
    report_path = write_capability_report(metrics)
    print(f"capability story2 report: {report_path}")


@pytest.mark.agent_live
@pytest.mark.vllm_live
@pytest.mark.vector_agent_live
@pytest.mark.agent_capability
@pytest.mark.asyncio
@pytest.mark.timeout(7200)
async def test_vector_story3_clean_notify_cycle(
    skip_unless_vllm_live,
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Story 3 — notification REST with story3_clean overlay (no stdlib trap)."""
    skip_unless_vector_agent_live()
    if not docker_available():
        pytest.skip("docker not available")

    setup_vector_agent_env(monkeypatch)
    start_vector_stack()

    metrics = await _run_capability_cycle(
        ticket=vector_story3_ticket(),
        tmp_path=tmp_path,
        story_id="story3_notify_clean",
        test_target="tests/test_notify_routes.py",
        workspace_name="vector-story3-clean",
        overlay="story3_clean",
    )
    report_path = write_capability_report(metrics)
    print(f"capability story3 clean report: {report_path}")
