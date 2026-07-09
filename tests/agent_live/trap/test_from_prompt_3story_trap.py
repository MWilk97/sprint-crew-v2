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
)
from tests.helpers.from_prompt_live import (
    VECTOR_TRAP_PROMPT,
    run_from_prompt_live,
)
from tests.helpers.session_metrics import planning_mode_from_session
from tests.helpers.vector_tiers import (
    failure_class_from_session,
    last_gate_result,
    setup_vector_agent_env,
    skip_unless_vector_agent_live,
    start_vector_stack,
    trap_strict_mode,
    write_trap_report,
)
from tests.helpers.vllm_live import docker_available


def _backlog_failure_message(run, sessions: list[SprintSession]) -> str:
    parts = [
        f"status={run.status.value}",
        f"error={run.error!r}",
        f"failed_ticket_key={getattr(run, 'failed_ticket_key', None)}",
    ]
    for session in sessions:
        gate = last_gate_result(session)
        fc = failure_class_from_session(session)
        parts.append(
            f"{session.ticket_key}: status={session.status.value} "
            f"gate={gate.get('accepted')} failure_class={fc}"
        )
    return "; ".join(parts)


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
    """Full 3-story from-prompt with stdlib trap on story 3 — SOFT unless VECTOR_TRAP_STRICT=1."""
    skip_unless_vector_agent_live()
    if not docker_available():
        pytest.skip("docker not available")

    setup_vector_agent_env(monkeypatch)
    start_vector_stack()

    started = time.perf_counter()
    result = None
    failure: str | None = None
    report_payload: dict | None = None
    try:
        with skip_template_fast_path():
            result = await run_from_prompt_live(
                prompt=VECTOR_TRAP_PROMPT,
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
        elif not (2 <= len(plan.stories) <= 3):
            failure = f"expected 2-3 stories, got {len(plan.stories)}"
        elif len(sessions) != len(plan.stories):
            failure = f"session count {len(sessions)} != story count {len(plan.stories)}"

        session_metrics: list[dict] = []
        total_semantic = 0

        if failure is None:
            pre_hits = semantic_search(result.scrum_workspace_id, VECTOR_TRAP_PROMPT, top_k=5)
            if not any("ferry" in hit.path for hit in pre_hits):
                failure = f"semantic index missing ferry.py: {pre_hits}"

        for session in sessions:
            workspace = Path(session.workspace_root)
            ticket = session.selected_ticket
            semantic_calls = count_semantic_retrieval_events(session)
            total_semantic += semantic_calls
            fc = failure_class_from_session(session)
            session_metrics.append(
                {
                    "ticket_key": session.ticket_key,
                    "status": session.status.value,
                    "failure_class": fc,
                    "semantic_tool_calls": semantic_calls,
                    "index_chunks": index_chunks_from_session(session),
                    "planning_mode": planning_mode_from_session(session),
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

        report_payload = {
            "tier": "trap",
            "trap": "from_prompt_3story",
            "duration_s": round(duration_s, 2),
            "story_count": len(plan.stories) if result else 0,
            "backlog_status": run.status.value if result else "unknown",
            "failed_ticket_key": getattr(run, "failed_ticket_key", None) if result else None,
            "total_semantic_tool_calls": total_semantic,
            "sessions": session_metrics,
            "failure": failure,
        }
    except Exception as exc:
        duration_s = time.perf_counter() - started
        failure = str(exc)
        report_payload = {
            "tier": "trap",
            "trap": "from_prompt_3story",
            "duration_s": round(duration_s, 2),
            "failure": failure,
            "sessions": [],
        }
        if trap_strict_mode():
            raise
    finally:
        if report_payload is not None:
            trap_path = write_trap_report(report_payload)
            print(f"trap 3-story report: {trap_path}")

    if failure is not None and trap_strict_mode():
        pytest.fail(failure)
    if failure is not None:
        print(f"trap SOFT pass-with-failure: {failure}")
