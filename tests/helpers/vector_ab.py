from __future__ import annotations

import json
import os
import subprocess
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from sprint_crew.config import get_settings
from sprint_crew.orchestrator.merge_gate import review_accepted
from sprint_crew.schemas.session import SessionStatus, SprintSession
from tests.helpers.cycle_assertions import (
    count_semantic_search_events,
    count_tool_call_events,
    index_chunks_from_session,
)
from tests.helpers.session_metrics import planning_mode_from_session


@dataclass
class CycleVectorMetrics:
    vector_enabled: bool
    duration_s: float
    session_status: str
    planning_mode: str | None
    index_chunks: int | None
    semantic_tool_calls: int
    total_tool_calls: int
    files_to_touch: list[str]
    review_passed: bool
    tests_passed: bool
    merge_gate_ok: bool


def copy_fixture_workspace(
    fixture_repo_path: Path, tmp_path: Path, name: str | None = None
) -> Path:
    """Copy greeter fixture into tmp_path and init git (same as tmp_workspace fixture)."""
    dest = tmp_path / (name or f"workspace-{uuid4().hex[:8]}")
    subprocess.run(
        ["cp", "-a", str(fixture_repo_path), str(dest)],
        check=True,
        capture_output=True,
    )
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "sprint-crew",
        "GIT_AUTHOR_EMAIL": "sprint-crew@local",
        "GIT_COMMITTER_NAME": "sprint-crew",
        "GIT_COMMITTER_EMAIL": "sprint-crew@local",
    }
    subprocess.run(["git", "init"], cwd=dest, check=True, capture_output=True)
    subprocess.run(["git", "add", "-A"], cwd=dest, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "init workspace"],
        cwd=dest,
        check=True,
        capture_output=True,
        env=env,
    )
    return dest


def collect_cycle_metrics(
    session: SprintSession, *, vector_enabled: bool, duration_s: float
) -> CycleVectorMetrics:
    review = session.review_outcome
    files: list[str] = []
    if session.task_plan is not None:
        files = list(session.task_plan.files_to_touch)

    review_passed = bool(review and review.passed)
    tests_passed = bool(review and review.tests_passed)
    merge_gate_ok = bool(
        review and review_accepted(review) and session.status == SessionStatus.AWAITING_HUMAN
    )

    return CycleVectorMetrics(
        vector_enabled=vector_enabled,
        duration_s=round(duration_s, 2),
        session_status=session.status.value,
        planning_mode=planning_mode_from_session(session),
        index_chunks=index_chunks_from_session(session),
        semantic_tool_calls=count_semantic_search_events(session),
        total_tool_calls=count_tool_call_events(session),
        files_to_touch=files,
        review_passed=review_passed,
        tests_passed=tests_passed,
        merge_gate_ok=merge_gate_ok,
    )


def compare_vector_ab(baseline: CycleVectorMetrics, vector_on: CycleVectorMetrics) -> dict:
    baseline_files = set(baseline.files_to_touch)
    vector_files = set(vector_on.files_to_touch)
    return {
        "baseline": asdict(baseline),
        "vector_on": asdict(vector_on),
        "delta": {
            "semantic_tool_calls": vector_on.semantic_tool_calls - baseline.semantic_tool_calls,
            "total_tool_calls": vector_on.total_tool_calls - baseline.total_tool_calls,
            "duration_s": round(vector_on.duration_s - baseline.duration_s, 2),
            "files_only_baseline": sorted(baseline_files - vector_files),
            "files_only_vector": sorted(vector_files - baseline_files),
            "files_shared": sorted(baseline_files & vector_files),
        },
    }


def write_vector_ab_report(comparison: dict, *, results_dir: Path | None = None) -> Path:
    root = get_settings().project_root
    out_dir = results_dir or (root / "benchmarks" / "results")
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    path = out_dir / f"vector_ab_{stamp}.json"
    path.write_text(json.dumps(comparison, indent=2) + "\n", encoding="utf-8")
    return path
