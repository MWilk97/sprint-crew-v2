from __future__ import annotations

import shutil
import subprocess
from collections.abc import Iterator
from pathlib import Path
from uuid import uuid4

import pytest

from sprint_crew.config import Settings, get_settings
from sprint_crew.integrations.jira_client import get_jira_client
from sprint_crew.orchestrator.session import prepare_workspace

TEST_SUMMARY_PREFIX = "[sprint-crew-test]"


def auth_github_repo_url(settings: Settings, repo_slug: str | None = None) -> str:
    slug = repo_slug or settings.github_repo
    return f"https://x-access-token:{settings.github_token}@github.com/{slug}.git"


def delete_remote_branch(settings: Settings, branch: str, *, repo_slug: str | None = None) -> None:
    repo_url = auth_github_repo_url(settings, repo_slug)
    subprocess.run(
        ["git", "push", repo_url, "--delete", branch],
        capture_output=True,
        text=True,
        check=False,
    )


def close_pull_requests_for_branch(
    settings: Settings, branch: str, *, repo_slug: str | None = None
) -> None:
    from github import Auth, Github

    slug = repo_slug or settings.github_repo
    owner, repo_name = slug.split("/", 1)
    gh = Github(auth=Auth.Token(settings.github_token))
    repo = gh.get_repo(f"{owner}/{repo_name}")
    for pr in repo.get_pulls(state="open", head=f"{owner}:{branch}"):
        pr.edit(state="closed")


@pytest.fixture
def integration_live_env(skip_unless_integration_live) -> Settings:
    settings = get_settings()
    if settings.use_mock_integrations:
        pytest.skip("USE_MOCK_INTEGRATIONS must be false for integration_live")
    if not settings.jira_url or not settings.jira_email or not settings.jira_api_token:
        pytest.skip("Jira credentials not configured")
    if not settings.github_token or not settings.github_repo:
        pytest.skip("GITHUB_TOKEN / GITHUB_REPO not configured")
    return settings


@pytest.fixture
def live_jira(integration_live_env: Settings):
    return get_jira_client()


@pytest.fixture
def sandbox_workspace(integration_live_env: Settings) -> Iterator[Path]:
    session_id = f"integration-live-{uuid4().hex[:8]}"
    repo_url = auth_github_repo_url(integration_live_env)
    workspace = prepare_workspace(session_id, repo_url=repo_url)
    subprocess.run(
        ["git", "remote", "set-url", "origin", repo_url],
        cwd=workspace,
        capture_output=True,
        text=True,
        check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "sprint-crew"],
        cwd=workspace,
        capture_output=True,
        check=True,
    )
    subprocess.run(
        ["git", "config", "user.email", "sprint-crew@local"],
        cwd=workspace,
        capture_output=True,
        check=True,
    )
    yield workspace
    shutil.rmtree(workspace, ignore_errors=True)


@pytest.fixture
def cleanup_ship_artifacts(integration_live_env: Settings) -> Iterator[list[str]]:
    branches: list[str] = []
    yield branches
    for branch in branches:
        close_pull_requests_for_branch(integration_live_env, branch)
        delete_remote_branch(integration_live_env, branch)


@pytest.fixture
def integration_vllm_env(skip_unless_vllm_live, skip_unless_integration_live) -> Settings:
    settings = get_settings()
    if settings.use_mock_integrations:
        pytest.skip("USE_MOCK_INTEGRATIONS must be false for integration+vLLM live test")
    if not settings.jira_url or not settings.jira_email or not settings.jira_api_token:
        pytest.skip("Jira credentials not configured")
    if not settings.github_token or not settings.github_repo:
        pytest.skip("GITHUB_TOKEN / GITHUB_REPO not configured")
    fixture_slug = settings.github_fixture_repo_greeter
    if not fixture_slug:
        pytest.skip(
            "GITHUB_FIXTURE_REPO_GREETER not configured (run scripts/bootstrap_fixture_repos.sh)"
        )
    return settings
