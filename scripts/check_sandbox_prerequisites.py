#!/usr/bin/env python3
"""Validate sandbox Jira/GitHub prerequisites before integration or GX10 tests."""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


def _fail(msg: str) -> None:
    print(f"FAIL: {msg}", file=sys.stderr)


def _ok(msg: str) -> None:
    print(f"OK  {msg}")


def _require_bin(name: str) -> bool:
    if shutil.which(name) is None:
        _fail(f"required command not found: {name}")
        return False
    _ok(f"command {name}")
    return True


def _check_venv() -> bool:
    venv_python = ROOT / ".venv" / "bin" / "python"
    if not venv_python.is_file():
        _fail(
            f"project venv missing: {venv_python} (run: python3 -m venv .venv && pip install -e '.[dev]')"
        )
        return False
    _ok("project .venv")
    return True


def _check_env_file() -> bool:
    env_path = ROOT / ".env"
    if not env_path.is_file():
        _fail(".env missing — copy .env.example and fill sandbox credentials")
        return False
    _ok(".env present")
    return True


def _check_integration_settings(*, require_fixture: bool) -> tuple[bool, object]:
    from sprint_crew.config import get_settings

    get_settings.cache_clear()
    settings = get_settings()

    ok = True
    if settings.use_mock_integrations:
        _fail("USE_MOCK_INTEGRATIONS must be false for sandbox tests")
        ok = False
    else:
        _ok("USE_MOCK_INTEGRATIONS=false")

    for name, value in (
        ("JIRA_URL", settings.jira_url),
        ("JIRA_EMAIL", settings.jira_email),
        ("JIRA_API_TOKEN", settings.jira_api_token),
        ("JIRA_PROJECT_KEY", settings.jira_project_key),
        ("JIRA_REVIEW_TRANSITION", settings.jira_review_transition),
        ("GITHUB_TOKEN", settings.github_token),
        ("GITHUB_REPO", settings.github_repo),
    ):
        if not value:
            _fail(f"{name} not configured in .env")
            ok = False
        else:
            _ok(f"{name} set")

    fixture_slug = settings.github_fixture_repo_greeter
    if require_fixture:
        if not fixture_slug:
            _fail(
                "GITHUB_FIXTURE_REPO_GREETER not configured "
                "(run scripts/bootstrap_fixture_repos.sh after setting slug in .env)"
            )
            ok = False
        else:
            _ok(f"fixture repo slug {fixture_slug}")

    return ok, settings


def _check_jira(settings) -> bool:
    from sprint_crew.integrations.jira_client import get_jira_client

    try:
        jira = get_jira_client()
        ticket = jira.create_issue(
            project_key=settings.jira_project_key,
            summary="[sprint-crew-test] prerequisite check",
            description="Automated sandbox prerequisite validation.",
            acceptance_criteria="pytest -q passes",
        )
        _ok(f"Jira create_issue -> {ticket.key}")
        jira.transition(ticket.key, settings.jira_review_transition)
        _ok(f"Jira transition -> {settings.jira_review_transition!r}")
    except Exception as exc:
        _fail(f"Jira API: {exc}")
        return False
    return True


def _check_github_clone(settings, repo_slug: str) -> bool:
    tmp = tempfile.mkdtemp(prefix="sprint-sandbox-check-")
    try:
        url = f"https://x-access-token:{settings.github_token}@github.com/{repo_slug}.git"
        proc = subprocess.run(
            ["git", "clone", "--depth", "1", url, tmp],
            capture_output=True,
            text=True,
            check=False,
        )
        if proc.returncode != 0:
            _fail(f"git clone {repo_slug}: {proc.stderr.strip() or proc.stdout.strip()}")
            return False
        _ok(f"git clone {repo_slug}")
        return True
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


_SKIP_BASELINE_NAMES = frozenset(
    {".git", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache"}
)


def _fixture_top_level_names(directory: Path) -> list[str]:
    return sorted(
        p.name
        for p in directory.iterdir()
        if p.name not in _SKIP_BASELINE_NAMES and not p.name.endswith(".pyc")
    )


def _check_fixture_baseline(settings, repo_slug: str) -> bool:
    fixture_dir = ROOT / "fixtures" / "repo"
    if not fixture_dir.is_dir():
        _fail(f"local fixture missing: {fixture_dir}")
        return False

    tmp = tempfile.mkdtemp(prefix="sprint-fixture-check-")
    try:
        url = f"https://x-access-token:{settings.github_token}@github.com/{repo_slug}.git"
        proc = subprocess.run(
            ["git", "clone", "--depth", "1", url, tmp],
            capture_output=True,
            text=True,
            check=False,
        )
        if proc.returncode != 0:
            _fail(f"fixture repo clone failed: {proc.stderr.strip()}")
            return False

        expected = _fixture_top_level_names(fixture_dir)
        actual = _fixture_top_level_names(Path(tmp))
        if expected != actual:
            _fail(
                f"fixture repo {repo_slug} main differs from fixtures/repo "
                f"(expected {expected}, got {actual}) — re-run bootstrap_fixture_repos.sh"
            )
            return False
        _ok(f"fixture repo {repo_slug} matches fixtures/repo baseline")
        return True
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _check_gx10() -> bool:
    ok = True
    if shutil.which("docker") is None:
        _fail("docker not found (required for GX10 tests)")
        ok = False
    else:
        proc = subprocess.run(["docker", "info"], capture_output=True, check=False)
        if proc.returncode != 0:
            _fail("docker info failed — is the daemon running?")
            ok = False
        else:
            _ok("docker daemon")

    from sprint_crew.config import get_settings

    settings = get_settings()
    if not settings.hf_token:
        _fail("HF_TOKEN not configured (required for vLLM model download)")
        ok = False
    else:
        _ok("HF_TOKEN set")

    lane_ctl = ROOT / "scripts" / "lane-ctl.sh"
    if not lane_ctl.is_file():
        _fail(f"missing {lane_ctl}")
        ok = False
    else:
        _ok("lane-ctl.sh present")

    return ok


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--gx10",
        action="store_true",
        help="Also validate Docker, HF_TOKEN, and lane tooling for GPU tests",
    )
    parser.add_argument(
        "--require-fixture",
        action="store_true",
        help="Require GITHUB_FIXTURE_REPO_GREETER and verify fixtures/repo baseline",
    )
    parser.add_argument(
        "--skip-jira-smoke",
        action="store_true",
        help="Skip live Jira create/transition (env-only checks)",
    )
    args = parser.parse_args()

    ok = True
    for cmd in ("git", "curl", "python3"):
        ok = _require_bin(cmd) and ok
    ok = _check_venv() and ok
    ok = _check_env_file() and ok

    if not ok:
        return 1

    settings_ok, settings = _check_integration_settings(require_fixture=args.require_fixture)
    ok = settings_ok and ok
    if not ok:
        return 1

    if not args.skip_jira_smoke:
        ok = _check_jira(settings) and ok

    ok = _check_github_clone(settings, settings.github_repo) and ok

    fixture_slug = settings.github_fixture_repo_greeter
    if fixture_slug:
        ok = _check_github_clone(settings, fixture_slug) and ok
        if args.require_fixture:
            ok = _check_fixture_baseline(settings, fixture_slug) and ok

    if args.gx10:
        ok = _check_gx10() and ok

    if ok:
        print("PASS: sandbox prerequisites satisfied")
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
