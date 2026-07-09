from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path
from uuid import uuid4

import pytest

from sprint_crew.config import get_settings
from sprint_crew.orchestrator.session import create_and_run_cycle
from tests.helpers.ac_targets import pytest_target_from_ac
from tests.helpers.agent_live_tickets import complex_api_ticket, skip_template_fast_path
from tests.helpers.cycle_assertions import assert_cycle_passed
from tests.helpers.vector_ab import (
    CycleVectorMetrics,
    collect_cycle_metrics,
    compare_vector_ab,
    copy_fixture_workspace,
    write_vector_ab_report,
)
from tests.helpers.vector_live import wait_vector_healthy
from tests.helpers.vector_tiers import skip_unless_vector_agent_live
from tests.helpers.vllm_live import docker_available

_ROOT = Path(__file__).resolve().parents[2]
_LANE_CTL = _ROOT / "scripts" / "lane-ctl.sh"

# Per-cycle wall-clock budget (seconds). Two cycles, each retried once on
# non-convergence, must fit the test timeout below: 4 * BUDGET < timeout.
_CYCLE_BUDGET_S = float(os.environ.get("VECTOR_AB_CYCLE_BUDGET_S", "2400"))


def _failed_metrics(*, vector_enabled: bool, duration_s: float) -> CycleVectorMetrics:
    return CycleVectorMetrics(
        vector_enabled=vector_enabled,
        duration_s=round(duration_s, 2),
        session_status="error",
        planning_mode=None,
        index_chunks=None,
        semantic_tool_calls=0,
        total_tool_calls=0,
        files_to_touch=[],
        review_passed=False,
        tests_passed=False,
        merge_gate_ok=False,
    )


@pytest.mark.agent_live
@pytest.mark.vllm_live
@pytest.mark.vector_agent_live
@pytest.mark.asyncio
@pytest.mark.timeout(10800)
async def test_sprint_cycle_vector_ab(
    skip_unless_vllm_live,
    fixture_repo_path,
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Compare full sprint cycle with vector index off vs on (COMPLEX ticket).

    Both cycles always run (per-cycle wall-clock budget prevents one cycle from
    starving the other), a non-converging cycle is retried once, and a report is
    always written. The test hard-fails only when BOTH cycles fail to reach the
    merge gate after their retry — a single non-convergence is model variance.
    """
    skip_unless_vector_agent_live()
    if not docker_available():
        pytest.skip("docker not available")

    monkeypatch.setenv("MAX_TECHLEAD_TURNS", "24")
    get_settings.cache_clear()

    settings = get_settings()
    # Clean lane state — pipeline starts work/coder on demand (one GPU lane at a time).
    subprocess.run([str(_LANE_CTL), "stop", "all"], check=False)

    ticket = complex_api_ticket()
    test_target = pytest_target_from_ac(ticket.acceptance_criteria)

    async def _run_once(*, vector_enabled: bool):
        monkeypatch.setenv("VECTOR_INDEX_ENABLED", "true" if vector_enabled else "false")
        get_settings.cache_clear()

        if vector_enabled:
            subprocess.run([str(_LANE_CTL), "start", "vector"], check=True)
            wait_vector_healthy(
                qdrant_url=settings.qdrant_url,
                embed_url=settings.embed_url.replace("/v1", ""),
            )

        workspace = copy_fixture_workspace(
            fixture_repo_path,
            tmp_path,
            name=f"ab-{'vector' if vector_enabled else 'baseline'}-{uuid4().hex[:6]}",
        )
        session_id = f"vector-ab-{uuid4().hex[:10]}"

        started = time.perf_counter()
        try:
            with skip_template_fast_path():
                session = await create_and_run_cycle(
                    ticket=ticket,
                    workspace=workspace,
                    session_id=session_id,
                    max_wall_seconds=_CYCLE_BUDGET_S,
                )
        except Exception as exc:  # noqa: BLE001 - record, do not abort the other cycle
            duration_s = time.perf_counter() - started
            print(f"cycle vector_enabled={vector_enabled} raised: {exc!r}")
            return (
                None,
                _failed_metrics(vector_enabled=vector_enabled, duration_s=duration_s),
                workspace,
            )
        duration_s = time.perf_counter() - started
        metrics = collect_cycle_metrics(
            session, vector_enabled=vector_enabled, duration_s=duration_s
        )
        return session, metrics, workspace

    async def _run_cycle_with_retry(*, vector_enabled: bool):
        """Run a cycle; retry once on a fresh workspace if it did not converge."""
        session, metrics, workspace = await _run_once(vector_enabled=vector_enabled)
        retried = False
        if not metrics.merge_gate_ok:
            label = "vector" if vector_enabled else "baseline"
            print(f"{label} cycle did not converge — retrying once (model variance policy)")
            retried = True
            session, metrics, workspace = await _run_once(vector_enabled=vector_enabled)
        return session, metrics, workspace, retried

    baseline_session, baseline_metrics, baseline_ws, baseline_retried = await _run_cycle_with_retry(
        vector_enabled=False
    )
    vector_session, vector_metrics, vector_ws, vector_retried = await _run_cycle_with_retry(
        vector_enabled=True
    )

    comparison = compare_vector_ab(baseline_metrics, vector_metrics)
    comparison["retry"] = {
        "cycle_budget_s": _CYCLE_BUDGET_S,
        "baseline_retried": baseline_retried,
        "vector_retried": vector_retried,
    }
    report_path = write_vector_ab_report(comparison)
    print(f"vector A/B report: {report_path}")

    baseline_ok = baseline_metrics.merge_gate_ok
    vector_ok = vector_metrics.merge_gate_ok

    # Contract: only a total failure (both cycles) is a hard failure.
    if not baseline_ok and not vector_ok:
        pytest.fail(
            "both A/B cycles failed to reach the merge gate after retry: "
            f"baseline status={baseline_metrics.session_status} "
            f"error={baseline_session.error if baseline_session else 'exception'}; "
            f"vector status={vector_metrics.session_status} "
            f"error={vector_session.error if vector_session else 'exception'}"
        )

    # Every cycle that DID converge must still satisfy the full assertions —
    # a converged cycle that fails these is a real regression, not variance.
    if baseline_ok:
        assert baseline_session is not None
        assert_cycle_passed(baseline_session, workspace=baseline_ws, test_target=test_target)
    if vector_ok:
        assert vector_session is not None
        assert_cycle_passed(vector_session, workspace=vector_ws, test_target=test_target)
        assert vector_metrics.index_chunks is not None and vector_metrics.index_chunks > 0

    if not (baseline_ok and vector_ok):
        under = "baseline" if not baseline_ok else "vector"
        print(f"note: A/B produced a partial result — {under} cycle under-converged after retry")

    if (
        baseline_ok
        and vector_ok
        and vector_metrics.semantic_tool_calls < baseline_metrics.semantic_tool_calls
    ):
        print(
            "note: vector run had fewer semantic_search calls than baseline "
            f"({vector_metrics.semantic_tool_calls} vs {baseline_metrics.semantic_tool_calls})"
        )
