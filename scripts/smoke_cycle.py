#!/usr/bin/env python3
"""Smoke test: full sprint cycle or coder-only diff + pytest."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from uuid import uuid4

# Run as a script (`scripts/smoke_cycle.py`), so the repo root is not on sys.path — only
# pytest gets that from pyproject's `pythonpath`. Needed for the tests.helpers import below.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tests.helpers.cycle_assertions import assert_cycle_passed

from sprint_crew.agents.coder import run_coder_loop
from sprint_crew.agents.formatter import run_formatter
from sprint_crew.config import Role, lane_for_role
from sprint_crew.graph.lanes import ensure_lane, stop_lane
from sprint_crew.orchestrator.session import create_and_run_cycle, prepare_workspace
from sprint_crew.schemas.change import CodeChange
from sprint_crew.schemas.ticket import JiraTicket, TaskPlan


def _wait_for_lanes(*roles: str, timeout: int = 600) -> bool:
    role_map = {"coder": Role.CODING, "work": Role.WORK}
    deadline = time.time() + timeout
    while time.time() < deadline:
        ok = True
        for name in roles:
            role = role_map[name]
            lane = lane_for_role(role)
            base = lane.base_url.rstrip("/")
            if base.endswith("/v1"):
                base = base[:-3]
            url = f"{base}/health"
            try:
                with urllib.request.urlopen(url, timeout=3) as resp:
                    if resp.status != 200:
                        ok = False
            except (urllib.error.URLError, TimeoutError, OSError):
                ok = False
        if ok:
            return True
        time.sleep(5)
    return False


def _normalize_change(change: CodeChange, task_plan: TaskPlan) -> CodeChange:
    branch = f"feature/{task_plan.ticket_key.lower()}"
    updates: dict[str, object] = {}
    if not change.ticket_key:
        updates["ticket_key"] = task_plan.ticket_key
    if not change.branch:
        updates["branch"] = branch
    return change.model_copy(update=updates) if updates else change


def _git_diff_nonempty(repo: Path) -> bool:
    proc = subprocess.run(
        ["git", "diff", "--stat"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
    )
    return bool(proc.stdout.strip())


def _pytest_passes(repo: Path, commands: list[str] | None = None) -> bool:
    """Run plan acceptance_tests when provided; default greeter path (shared fixture also has email stubs)."""
    cmds = commands or ["pytest -q tests/test_greeter.py"]
    ok = True
    for cmd in cmds:
        argv = shlex.split(cmd)
        proc = subprocess.run(
            argv,
            cwd=repo,
            capture_output=True,
            text=True,
            check=False,
        )
        print(proc.stdout)
        if proc.stderr:
            print(proc.stderr, file=sys.stderr)
        if proc.returncode != 0:
            ok = False
    return ok


def _prepare_repo(source: Path, dest: Path) -> None:
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(source, dest)
    subprocess.run(["git", "init"], cwd=dest, check=True, capture_output=True)
    subprocess.run(["git", "add", "-A"], cwd=dest, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "init fixture"],
        cwd=dest,
        check=True,
        capture_output=True,
        env={
            **os.environ,
            "GIT_AUTHOR_NAME": "smoke",
            "GIT_AUTHOR_EMAIL": "smoke@local",
            "GIT_COMMITTER_NAME": "smoke",
            "GIT_COMMITTER_EMAIL": "smoke@local",
        },
    )


async def run_coder_only(repo: Path, plan_path: Path) -> int:
    plan = TaskPlan.model_validate(json.loads(plan_path.read_text(encoding="utf-8")))
    await ensure_lane(Role.CODING)
    raw_output, _tool_log = await run_coder_loop(plan, repo)
    await stop_lane(Role.CODING)
    await ensure_lane(Role.WORK)
    change = await run_formatter(task_plan=plan, raw_output=raw_output, git_diff="")
    change = _normalize_change(change, plan)
    print(f"CodeChange: ticket={change.ticket_key} tests_passed={change.tests_passed}")
    if not _git_diff_nonempty(repo):
        print("FAIL: git diff is empty")
        await stop_lane(Role.WORK)
        return 1
    if not _pytest_passes(repo, plan.acceptance_tests):
        print("FAIL: pytest did not pass")
        await stop_lane(Role.WORK)
        return 1
    print("PASS: non-empty diff and pytest green")
    await stop_lane(Role.WORK)
    return 0


async def run_full_cycle(
    *,
    repo: Path | None,
    fixture: Path,
    use_real_ship: bool,
    preflight_lanes: bool,
) -> int:
    ticket = JiraTicket(
        key="DEMO-1",
        summary="Add hello() to greeter module",
        description="Implement hello() returning 'hello' and ensure pytest passes.",
        status="To Do",
        issue_type="Story",
        acceptance_criteria="pytest -q passes",
    )
    # fixtures/repo is red on purpose: tests/test_validators.py targets a *different*
    # ticket the agent has not been asked to implement. An unscoped pytest can never be
    # green there, so the post-check is scoped to this story (see commit ceda3a6).
    test_target = "tests/test_greeter.py"
    if repo is None:
        repo = prepare_workspace(f"smoke-{uuid4().hex[:8]}", source=fixture)
        print(f"Using workspace: {repo}")

    docker_check = subprocess.run(["docker", "info"], capture_output=True, text=True, check=False)
    if docker_check.returncode != 0:
        print("FAIL: docker not available (required for on-demand lane startup)")
        return 1

    if preflight_lanes:
        print("Preflight: waiting for coder lane health (pipeline starts lanes on demand)...")
        subprocess.run(
            [str(Path(__file__).resolve().parent / "lane-ctl.sh"), "start", "coder"],
            check=False,
        )
        if not _wait_for_lanes("coder", timeout=900):
            print("FAIL: coder lane not healthy after 15 min")
            return 1
    else:
        print("Skipping lane pre-warm — pipeline will start lanes on demand (AGENTS.md §4).")

    session = await create_and_run_cycle(
        ticket=ticket,
        workspace=repo,
        use_real_ship=use_real_ship,
    )
    print(f"Session {session.session_id} status={session.status.value}")
    try:
        assert_cycle_passed(
            session,
            workspace=Path(session.workspace_root),
            test_target=test_target,
        )
    except AssertionError as exc:
        print(f"FAIL: {exc}")
        if session.review_outcome:
            review = session.review_outcome
            print(f"Review: passed={review.passed} tests_passed={review.tests_passed}")
        if session.error:
            print(f"Error: {session.error}")
        return 1
    print("PASS: full sprint cycle complete")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=None, help="Use existing git workspace")
    parser.add_argument(
        "--fixture",
        type=Path,
        default=Path(__file__).resolve().parent.parent / "fixtures" / "repo",
    )
    parser.add_argument(
        "--real-ship",
        action="store_true",
        help="Use real git push + GitHub/Jira (requires credentials)",
    )
    parser.add_argument(
        "--preflight-lanes",
        action="store_true",
        help="Start coder lane before the cycle (optional; default relies on on-demand lanes)",
    )
    parser.add_argument(
        "--coder-only",
        action="store_true",
        help="Run Coder + Formatter only (no full LangGraph cycle)",
    )
    parser.add_argument(
        "--plan",
        type=Path,
        default=Path(__file__).resolve().parent.parent
        / "fixtures"
        / "repo"
        / "simple_task_plan.json",
        help="TaskPlan JSON for --coder-only mode",
    )
    args = parser.parse_args()
    fixture = args.fixture.resolve()

    if args.coder_only:
        if args.repo:
            repo = args.repo.resolve()
        else:
            tmp = Path(tempfile.mkdtemp(prefix="smoke-coder-"))
            _prepare_repo(fixture, tmp)
            repo = tmp
            print(f"Using temp repo: {repo}")
        return asyncio.run(run_coder_only(repo, args.plan.resolve()))

    repo = args.repo.resolve() if args.repo else None
    return asyncio.run(
        run_full_cycle(
            repo=repo,
            fixture=fixture,
            use_real_ship=args.real_ship,
            preflight_lanes=args.preflight_lanes,
        )
    )


if __name__ == "__main__":
    sys.exit(main())
