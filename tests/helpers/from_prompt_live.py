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
from tests.helpers.vector_tiers import failure_class_from_session, last_gate_result
from sprint_crew.vector.context import enrich_repo_context_with_hits
from sprint_crew.vector.indexer import maybe_index_workspace
from sprint_crew.vector.search import semantic_search
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

POSTCHECK_QUERIES: dict[str, str] = {
    "ferry": "ferry dispatch outbound queue worker",
    "retry": "exponential backoff retry adapter handoff",
}


def postcheck_collection_id(run_id: str) -> str:
    """Dedicated Qdrant collection for integration post-check (avoids prod cleanup ids)."""
    if run_id.startswith("postcheck-"):
        return run_id
    return f"postcheck-{run_id}"


@dataclass
class PostCheckResult:
    collection_id: str
    fragments_found: dict[str, bool]
    hits_by_fragment: dict[str, list[tuple[str, float]]]
    chunks: int | None = None


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


def _index_prompt_for_fragments(fragments: tuple[str, ...]) -> str:
    queries = [POSTCHECK_QUERIES[f] for f in fragments if f in POSTCHECK_QUERIES]
    return " ".join(queries) if queries else POSTCHECK_QUERIES["ferry"]


def verify_prompt_surfaces_path(
    workspace: Path,
    run_id: str,
    *,
    fragments: tuple[str, ...] = ("ferry",),
    top_k: int = 5,
) -> PostCheckResult:
    """Re-index final workspace and search per fragment; used after backlog cleanup drops Qdrant."""
    collection_id = postcheck_collection_id(run_id)
    index_result = maybe_index_workspace(
        workspace,
        collection_id,
        prompt=_index_prompt_for_fragments(fragments),
    )
    chunks = index_result.chunks if index_result is not None else None

    fragments_found: dict[str, bool] = {}
    hits_by_fragment: dict[str, list[tuple[str, float]]] = {}
    missing: list[str] = []

    for fragment in fragments:
        query = POSTCHECK_QUERIES.get(fragment, fragment)
        hits = semantic_search(collection_id, query, top_k=top_k)
        hit_pairs = [(h.path, h.score) for h in hits]
        hits_by_fragment[fragment] = hit_pairs
        found = any(fragment in path for path, _score in hit_pairs)
        fragments_found[fragment] = found
        if not found:
            missing.append(fragment)

    if missing:
        raise AssertionError(
            f"semantic index should surface {missing!r}, "
            f"fragments_found={fragments_found}, hits_by_fragment={hits_by_fragment}"
        )

    return PostCheckResult(
        collection_id=collection_id,
        fragments_found=fragments_found,
        hits_by_fragment=hits_by_fragment,
        chunks=chunks,
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


def backlog_failure_message(run: BacklogRun, sessions: list[SprintSession]) -> str:
    parts = [
        f"status={run.status.value}",
        f"error={run.error!r}",
        f"failed_ticket_key={getattr(run, 'failed_ticket_key', None)}",
        f"completed={getattr(run, 'completed_session_ids', [])}",
        f"sessions={len(sessions)}",
    ]
    for session in sessions:
        gate = last_gate_result(session)
        fc = failure_class_from_session(session)
        parts.append(
            f"{session.ticket_key}: session_status={session.status.value} "
            f"gate={gate.get('accepted')} block_reason={gate.get('block_reason')} "
            f"coverage_satisfied={gate.get('coverage_satisfied')} failure_class={fc}"
        )
    return "; ".join(parts)
