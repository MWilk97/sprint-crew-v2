from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from sprint_crew.schemas.backlog import BacklogPlan
from sprint_crew.schemas.session import BacklogRun, BacklogRunStatus, SprintSession
from tests.helpers.ac_targets import pytest_target_from_ac
from tests.helpers.cycle_assertions import (
    assert_cycle_passed,
    count_semantic_retrieval_events,
    index_chunks_from_session,
    pytest_passes,
    semantic_retrieval_diagnostics,
)
from tests.helpers.from_prompt_live import (
    FromPromptLiveResult,
    PostCheckResult,
    backlog_failure_message,
    verify_prompt_surfaces_path,
)
from tests.helpers.session_metrics import planning_mode_from_session
from tests.helpers.vector_tiers import last_gate_result

PLAN_FRAGMENT_GROUPS: tuple[tuple[str, ...], ...] = (
    ("ferry", "queue"),
    ("retry",),
)


@dataclass
class IntegrationCheckResult:
    pipeline_ok: bool
    post_check_ok: bool | None
    failure_class: str | None
    failure: str | None
    post_check: dict | None
    sessions: list[dict]
    partial_pytest: dict | None
    duration_s: float
    story_count: int
    backlog_status: str
    failed_ticket_key: str | None
    total_semantic_tool_calls: int
    files_to_touch_union: list[str]
    scrum_workspace_id: str | None
    tier: str
    trap: str | None = None

    def to_report_payload(self) -> dict:
        payload: dict = {
            "tier": self.tier,
            "duration_s": round(self.duration_s, 2),
            "story_count": self.story_count,
            "backlog_status": self.backlog_status,
            "failed_ticket_key": self.failed_ticket_key,
            "pipeline_ok": self.pipeline_ok,
            "post_check_ok": self.post_check_ok,
            "failure_class": self.failure_class,
            "total_semantic_tool_calls": self.total_semantic_tool_calls,
            "files_to_touch_union": self.files_to_touch_union,
            "sessions": self.sessions,
            "scrum_workspace_id": self.scrum_workspace_id,
            "failure": self.failure,
        }
        if self.post_check is not None:
            payload["post_check"] = self.post_check
        if self.partial_pytest is not None:
            payload["partial_pytest"] = self.partial_pytest
        if self.trap is not None:
            payload["trap"] = self.trap
        return payload


def classify_integration_failure(
    run: BacklogRun,
    sessions: list[SprintSession],
    failure_msg: str | None,
) -> str:
    if failure_msg is None:
        return "none"
    msg = failure_msg.lower()
    err = (run.error or "").lower()

    if "request timed out" in msg or "request timed out" in err:
        return "infra_timeout"
    if "hits=[]" in failure_msg or "semantic index should surface" in failure_msg:
        return "post_check"
    if "expecting value" in msg or "jsondecodeerror" in err:
        return "reviewer_json"
    if (
        "coverage" in msg
        or "out-of-scope" in msg
        or "scope violation" in msg
        or "coverage" in err
        or "out-of-scope" in err
        or "scope violation" in err
    ):
        return "merge_gate_coverage"
    if "merge gate" in msg:
        return "merge_gate"
    if "semantic retrieval" in msg:
        return "semantic_retrieval"
    if "unexpected planning mode" in msg:
        return "planning_mode"
    if "vector_indexed" in msg or "index_chunks" in msg:
        return "vector_index"
    if "files_to_touch" in msg or "plan files" in msg:
        return "plan_quality"
    if run.status != BacklogRunStatus.COMPLETED:
        return "backlog_failed"
    if any(":" in failure_msg for s in sessions if s.ticket_key in failure_msg):
        return "cycle_assert"
    return "unknown"


def _post_check_to_dict(result: PostCheckResult) -> dict:
    return {
        "collection_id": result.collection_id,
        "fragments": result.fragments_found,
        "chunks": result.chunks,
        "hits": result.hits_by_fragment,
    }


def _collect_partial_pytest(sessions: list[SprintSession]) -> dict | None:
    if not sessions:
        return None
    session = sessions[-1]
    ticket = session.selected_ticket
    if ticket is None:
        return None
    workspace = Path(session.workspace_root)
    test_target = pytest_target_from_ac(ticket.acceptance_criteria)
    passed = pytest_passes(workspace, test_target=test_target)
    return {
        "ticket_key": session.ticket_key,
        "target": test_target,
        "passed": passed,
        "workspace": str(workspace),
    }


def _session_metrics(session: SprintSession, *, include_failure_class: bool = False) -> dict:
    metrics = {
        "session_id": session.session_id,
        "ticket_key": session.ticket_key,
        "planning_mode": planning_mode_from_session(session),
        "index_chunks": index_chunks_from_session(session),
        "semantic_tool_calls": count_semantic_retrieval_events(session),
        "semantic_diagnostics": semantic_retrieval_diagnostics(session),
        "status": session.status.value,
        "last_gate": last_gate_result(session),
    }
    if include_failure_class:
        from tests.helpers.vector_tiers import failure_class_from_session

        metrics["failure_class"] = failure_class_from_session(session)
    return metrics


def assert_pipeline_completed(
    run: BacklogRun,
    plan: BacklogPlan,
    sessions: list[SprintSession],
    *,
    story_count_exact: int | None = None,
    story_count_range: tuple[int, int] | None = None,
) -> str | None:
    if run.status != BacklogRunStatus.COMPLETED:
        return backlog_failure_message(run, sessions)
    story_count = len(plan.stories)
    if story_count_exact is not None and story_count != story_count_exact:
        return (
            f"expected exactly {story_count_exact} stories, got {story_count}: "
            f"{[s.summary for s in plan.stories]}"
        )
    if story_count_range is not None:
        lo, hi = story_count_range
        if not (lo <= story_count <= hi):
            return f"expected {lo}-{hi} stories, got {story_count}"
    if len(sessions) != story_count:
        return f"session count {len(sessions)} != story count {story_count}"
    return None


def assert_per_session_cycles(sessions: list[SprintSession]) -> str | None:
    for session in sessions:
        ticket = session.selected_ticket
        if ticket is None:
            return f"{session.ticket_key}: missing selected_ticket"
        workspace = Path(session.workspace_root)
        test_target = pytest_target_from_ac(ticket.acceptance_criteria)
        try:
            assert_cycle_passed(session, workspace=workspace, test_target=test_target)
        except AssertionError as exc:
            return f"{session.ticket_key}: {exc}"

        mode = planning_mode_from_session(session)
        if mode not in {"tool_loop", "static", "template_fallback"}:
            return f"{session.ticket_key}: unexpected planning mode={mode}"

        chunks = index_chunks_from_session(session)
        if chunks is None or chunks <= 0:
            return f"{session.ticket_key}: expected vector_indexed event with chunks"
    return None


def assert_per_session_semantic(
    sessions: list[SprintSession], *, min_per_story: int = 1
) -> str | None:
    for session in sessions:
        semantic_calls = count_semantic_retrieval_events(session)
        if semantic_calls < min_per_story:
            return (
                f"{session.ticket_key}: expected >= {min_per_story} semantic retrieval events, "
                f"got {semantic_calls}; {semantic_retrieval_diagnostics(session)}"
            )
    return None


def assert_plan_files_quality(files_to_touch: set[str]) -> str | None:
    union = " ".join(files_to_touch).lower()
    for group in PLAN_FRAGMENT_GROUPS:
        if not any(fragment in union for fragment in group):
            missing = "/".join(group)
            return (
                f"files_to_touch_union missing expected fragment group ({missing}); "
                f"got {sorted(files_to_touch)}"
            )
    return None


def run_post_check(
    final_workspace: Path,
    run_id: str,
    *,
    fragments: tuple[str, ...] = ("ferry", "retry"),
) -> PostCheckResult:
    return verify_prompt_surfaces_path(final_workspace, run_id, fragments=fragments)


def evaluate_from_prompt_run(
    result: FromPromptLiveResult,
    duration_s: float,
    *,
    tier: str = "integration",
    trap: str | None = None,
    story_count_exact: int | None = 2,
    story_count_range: tuple[int, int] | None = None,
    post_check_fragments: tuple[str, ...] = ("ferry", "retry"),
    include_trap_failure_class: bool = False,
    assert_plan_fragments: bool = True,
) -> IntegrationCheckResult:
    run = result.run
    plan = result.plan
    sessions = result.sessions

    failure: str | None = None
    post_check_ok: bool | None = None
    post_check_dict: dict | None = None
    partial_pytest: dict | None = None

    all_files: set[str] = set()
    session_metrics: list[dict] = []
    total_semantic = 0

    pipeline_failure = assert_pipeline_completed(
        run,
        plan,
        sessions,
        story_count_exact=story_count_exact,
        story_count_range=story_count_range,
    )
    pipeline_ok = pipeline_failure is None

    if pipeline_failure is not None:
        failure = pipeline_failure
        partial_pytest = _collect_partial_pytest(sessions)

    if pipeline_ok and sessions:
        final_workspace = Path(sessions[-1].workspace_root)
        try:
            post_result = run_post_check(
                final_workspace,
                run.run_id,
                fragments=post_check_fragments,
            )
            post_check_ok = True
            post_check_dict = _post_check_to_dict(post_result)
        except AssertionError as exc:
            failure = str(exc)
            post_check_ok = False

    for session in sessions:
        if session.task_plan is not None:
            all_files.update(session.task_plan.files_to_touch)
        total_semantic += count_semantic_retrieval_events(session)
        session_metrics.append(
            _session_metrics(session, include_failure_class=include_trap_failure_class)
        )

    if failure is None:
        failure = assert_per_session_cycles(sessions)

    if failure is None:
        failure = assert_per_session_semantic(sessions)

    if failure is None and assert_plan_fragments and tier == "integration":
        failure = assert_plan_files_quality(all_files)

    if failure is not None and partial_pytest is None and not pipeline_ok:
        partial_pytest = _collect_partial_pytest(sessions)

    failure_class = classify_integration_failure(run, sessions, failure)
    if failure is None:
        failure_class = None

    return IntegrationCheckResult(
        pipeline_ok=pipeline_ok,
        post_check_ok=post_check_ok,
        failure_class=failure_class,
        failure=failure,
        post_check=post_check_dict,
        sessions=session_metrics,
        partial_pytest=partial_pytest,
        duration_s=duration_s,
        story_count=len(plan.stories),
        backlog_status=run.status.value,
        failed_ticket_key=getattr(run, "failed_ticket_key", None),
        total_semantic_tool_calls=total_semantic,
        files_to_touch_union=sorted(all_files),
        scrum_workspace_id=result.scrum_workspace_id,
        tier=tier,
        trap=trap,
    )
