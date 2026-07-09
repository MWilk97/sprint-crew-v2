from __future__ import annotations

import time
from pathlib import Path

import pytest

from sprint_crew.schemas.session import BacklogRunStatus, SprintSession
from sprint_crew.vector.search import semantic_search
from tests.helpers.ac_targets import pytest_target_from_ac
from tests.helpers.agent_live_tickets import skip_template_fast_path
from tests.helpers.cycle_assertions import (
    assert_cycle_passed,
    count_semantic_retrieval_events,
    index_chunks_from_session,
    semantic_retrieval_diagnostics,
)
from tests.helpers.from_prompt_live import (
    VECTOR_INTEGRATION_PROMPT,
    run_from_prompt_live,
    write_from_prompt_integration_report,
)
from tests.helpers.session_metrics import planning_mode_from_session
from tests.helpers.vector_tiers import (
    last_gate_result,
    setup_vector_agent_env,
    skip_unless_vector_agent_live,
    start_vector_stack,
)
from tests.helpers.vllm_live import docker_available


def _backlog_failure_message(run, sessions: list[SprintSession]) -> str:
    parts = [
        f"status={run.status.value}",
        f"error={run.error!r}",
        f"failed_ticket_key={getattr(run, 'failed_ticket_key', None)}",
        f"completed={getattr(run, 'completed_session_ids', [])}",
        f"sessions={len(sessions)}",
    ]
    for session in sessions:
        gate = last_gate_result(session)
        parts.append(
            f"{session.ticket_key}: session_status={session.status.value} "
            f"gate={gate.get('accepted')} block_reason={gate.get('block_reason')} "
            f"coverage_satisfied={gate.get('coverage_satisfied')}"
        )
    return "; ".join(parts)


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
    result = None
    report_payload: dict | None = None
    failure: str | None = None
    try:
        with skip_template_fast_path():
            result = await run_from_prompt_live(
                prompt=VECTOR_INTEGRATION_PROMPT,
                fixture_path=fixture_vector_repo_path,
                tmp_path=tmp_path,
                use_real_ship=False,
            )
        duration_s = time.perf_counter() - started

        run = result.run
        plan = result.plan
        sessions = result.sessions

        if run.status != BacklogRunStatus.COMPLETED:
            failure = _backlog_failure_message(run, sessions)
        elif len(plan.stories) != 2:
            failure = (
                f"expected exactly 2 stories, got {len(plan.stories)}: "
                f"{[s.summary for s in plan.stories]}"
            )
        elif len(sessions) != len(plan.stories):
            failure = f"session count {len(sessions)} != story count {len(plan.stories)}"

        total_semantic = 0
        all_files: set[str] = set()
        session_metrics: list[dict] = []

        if failure is None:
            pre_hits = semantic_search(
                result.scrum_workspace_id,
                VECTOR_INTEGRATION_PROMPT,
                top_k=5,
            )
            if not any("ferry" in hit.path for hit in pre_hits):
                failure = (
                    "semantic index should surface ferry.py, "
                    f"hits={[(h.path, h.score) for h in pre_hits]}"
                )

        for session in sessions:
            workspace = Path(session.workspace_root)
            ticket = session.selected_ticket
            mode = planning_mode_from_session(session)
            chunks = index_chunks_from_session(session)
            semantic_calls = count_semantic_retrieval_events(session)
            total_semantic += semantic_calls
            if session.task_plan is not None:
                all_files.update(session.task_plan.files_to_touch)

            session_metrics.append(
                {
                    "session_id": session.session_id,
                    "ticket_key": session.ticket_key,
                    "planning_mode": mode,
                    "index_chunks": chunks,
                    "semantic_tool_calls": semantic_calls,
                    "semantic_diagnostics": semantic_retrieval_diagnostics(session),
                    "status": session.status.value,
                    "last_gate": last_gate_result(session),
                }
            )

            if failure is not None:
                continue

            assert ticket is not None
            test_target = pytest_target_from_ac(ticket.acceptance_criteria)
            try:
                assert_cycle_passed(session, workspace=workspace, test_target=test_target)
            except AssertionError as exc:
                failure = f"{session.ticket_key}: {exc}"
                break

            if mode not in {"tool_loop", "static", "template_fallback"}:
                failure = f"{session.ticket_key}: unexpected planning mode={mode}"
                break
            if chunks is None or chunks <= 0:
                failure = f"{session.ticket_key}: expected vector_indexed event with chunks"
                break

        if failure is None and total_semantic < 1:
            diag = "; ".join(m["semantic_diagnostics"] for m in session_metrics)
            failure = (
                "TechLead should use semantic retrieval at least once across stories; "
                f"diagnostics: {diag}"
            )

        report_payload = {
            "tier": "integration",
            "duration_s": round(duration_s, 2),
            "story_count": len(plan.stories) if result else 0,
            "backlog_status": run.status.value if result else "unknown",
            "failed_ticket_key": getattr(run, "failed_ticket_key", None) if result else None,
            "total_semantic_tool_calls": total_semantic,
            "files_to_touch_union": sorted(all_files),
            "sessions": session_metrics,
            "scrum_workspace_id": result.scrum_workspace_id if result else None,
            "failure": failure,
        }
    except Exception as exc:
        duration_s = time.perf_counter() - started
        failure = str(exc)
        report_payload = {
            "tier": "integration",
            "duration_s": round(duration_s, 2),
            "failure": failure,
            "sessions": [],
        }
        raise
    finally:
        if report_payload is not None:
            report_path = write_from_prompt_integration_report(report_payload)
            print(f"from-prompt integration report: {report_path}")

    if failure is not None:
        pytest.fail(failure)

    expected_fragments = ("ferry", "retry")
    found = [frag for frag in expected_fragments if any(frag in f for f in all_files)]
    if len(found) < 1:
        print(f"note: plan files_to_touch union may be incomplete: {sorted(all_files)}")
