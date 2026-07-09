from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from sprint_crew.agents.scrum_master import run_scrum_master
from sprint_crew.config import Role, get_settings
from sprint_crew.graph.lanes import ensure_lane, stop_lane
from sprint_crew.orchestrator.backlog import BacklogRunStore, normalize_backlog_plan, run_backlog
from sprint_crew.orchestrator.session import SessionStore, prepare_workspace
from sprint_crew.schemas.backlog import BacklogPlan
from sprint_crew.schemas.session import BacklogRun, BacklogRunStatus, SprintSession
from sprint_crew.vector.context import enrich_repo_context_with_hits
from sprint_crew.vector.indexer import maybe_index_workspace
from tests.helpers.vector_ab import copy_fixture_workspace

VECTOR_INTEGRATION_PROMPT = """\
We have an internal task platform with SQLite storage and partial HTTP routes.
Add a resilient outbound notification subsystem integrated with existing patterns.

Split into exactly 2 shippable stories (one PR each), dependency order:
1) Persistent outbound message queue using the existing ferry dispatch layer
2) Exponential backoff retry (3 attempts, 100ms base) when adapter handoff fails

Each story must pass its dedicated pytest module without breaking greeter tests.
Follow existing architecture; discover integration points from the repo, not guesses.
"""

VECTOR_TRAP_PROMPT = """\
We have an internal task platform with SQLite storage and partial HTTP routes.
Add a resilient outbound notification subsystem integrated with existing patterns.

Split into 2-3 shippable stories (one PR each), dependency order:
1) Persistent outbound message queue using the existing ferry dispatch layer
2) Exponential backoff retry (3 attempts, 100ms base) when adapter handoff fails
3) REST endpoints to enqueue notifications and query delivery status

Each story must pass its dedicated pytest module without breaking greeter tests.
Follow existing architecture; discover integration points from the repo, not guesses.
"""


def git_file_url(workspace: Path) -> str:
    return workspace.resolve().as_uri()


def init_vector_repo_git(
    fixture_path: Path, tmp_path: Path, *, name: str | None = None
) -> tuple[Path, str]:
    """Copy vector fixture, init git, return workspace path and file:// clone URL."""
    dest = copy_fixture_workspace(
        fixture_path, tmp_path, name=name or f"vector-repo-{uuid4().hex[:8]}"
    )
    return dest, git_file_url(dest)


@dataclass
class FromPromptLiveResult:
    run: BacklogRun
    plan: BacklogPlan
    sessions: list[SprintSession]
    scrum_workspace_id: str


async def run_from_prompt_live(
    *,
    prompt: str,
    fixture_path: Path,
    tmp_path: Path,
    run_id: str | None = None,
    use_real_ship: bool = False,
) -> FromPromptLiveResult:
    """Mirror POST /sprint/from-prompt orchestration without HTTP background tasks."""
    settings = get_settings()
    run_id = run_id or str(uuid4())
    scrum_workspace_id = f"backlog-{run_id}"

    _git_root, repo_url = init_vector_repo_git(fixture_path, tmp_path, name=f"git-{run_id[:8]}")

    workspace = prepare_workspace(scrum_workspace_id, source=fixture_path)
    maybe_index_workspace(workspace, scrum_workspace_id, prompt=prompt)
    repo_context, _pre_hits = enrich_repo_context_with_hits(workspace, scrum_workspace_id, prompt)

    work_lane = Role.WORK
    await ensure_lane(work_lane)
    try:
        plan = await run_scrum_master(
            user_prompt=prompt,
            repo_context=repo_context,
            role=work_lane,
        )
    finally:
        await stop_lane(work_lane)

    plan = normalize_backlog_plan(plan, user_prompt=prompt)

    BacklogRunStore(settings.session_db).save(
        BacklogRun(
            run_id=run_id,
            status=BacklogRunStatus.PENDING,
            user_prompt=prompt,
            repo_url=repo_url,
        )
    )

    run = await run_backlog(
        run_id=run_id,
        plan=plan,
        user_prompt=prompt,
        repo_url=repo_url,
        use_real_ship=use_real_ship,
    )

    session_store = SessionStore(settings.session_db)
    sessions = []
    for session_id in run.session_ids:
        loaded = session_store.load(session_id)
        if loaded is not None:
            sessions.append(loaded)

    return FromPromptLiveResult(
        run=run,
        plan=plan,
        sessions=sessions,
        scrum_workspace_id=scrum_workspace_id,
    )


def _write_benchmark_report(prefix: str, payload: dict, *, results_dir: Path | None = None) -> Path:
    root = get_settings().project_root
    out_dir = results_dir or (root / "benchmarks" / "results")
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    path = out_dir / f"{prefix}_{stamp}.json"
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return path


def write_from_prompt_integration_report(payload: dict, *, results_dir: Path | None = None) -> Path:
    return _write_benchmark_report("from_prompt_integration", payload, results_dir=results_dir)
