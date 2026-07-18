from __future__ import annotations

import logging
import os
import re
import subprocess
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any

from sprint_crew.config import get_settings
from sprint_crew.schemas.ticket import JiraTicket

log = logging.getLogger(__name__)

_REDACT_PATTERNS = (
    re.compile(r"(?i)(api[_-]?token|authorization|password)\s*[:=]\s*\S+"),
    re.compile(r"ghp_[A-Za-z0-9]+"),
    re.compile(r"gho_[A-Za-z0-9]+"),
)

_AC_SECTION_RE = re.compile(
    r"(?is)acceptance\s+criteria\s*:\s*(.+?)(?:\n\n[A-Z][^\n]{0,40}:|\Z)",
)


def redact_secrets(text: str) -> str:
    result = text
    for pattern in _REDACT_PATTERNS:
        result = pattern.sub("[REDACTED]", result)
    return result


def _adf_to_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        if value.get("type") == "doc":
            parts: list[str] = []
            for block in value.get("content", []):
                for node in block.get("content", []):
                    text = node.get("text")
                    if text:
                        parts.append(text)
            return "\n".join(parts).strip()
        return str(value.get("value", value))
    return str(value)


def parse_acceptance_criteria_from_description(description: str) -> str:
    if not description.strip():
        return ""
    match = _AC_SECTION_RE.search(description)
    if match:
        return match.group(1).strip()
    return ""


def extract_acceptance_criteria(fields: dict[str, Any], *, description: str) -> str:
    settings = get_settings()
    if settings.jira_ac_field:
        raw = fields.get(settings.jira_ac_field)
        text = _adf_to_text(raw).strip()
        if text:
            return text
    return parse_acceptance_criteria_from_description(description)


class JiraClient(ABC):
    @abstractmethod
    def get_ticket(self, key: str) -> JiraTicket:
        raise NotImplementedError

    @abstractmethod
    def create_issue(
        self,
        *,
        project_key: str,
        summary: str,
        description: str = "",
        issue_type: str = "Story",
        acceptance_criteria: str = "",
    ) -> JiraTicket:
        raise NotImplementedError

    @abstractmethod
    def add_comment(self, key: str, body: str) -> None:
        raise NotImplementedError

    @abstractmethod
    def transition(self, key: str, status: str) -> None:
        raise NotImplementedError


class GitHubClient(ABC):
    @abstractmethod
    def create_pull_request(
        self,
        *,
        title: str,
        body: str,
        head: str,
        base: str = "main",
    ) -> str:
        raise NotImplementedError


class AtlassianJiraClient(JiraClient):
    def __init__(self) -> None:
        settings = get_settings()
        if not settings.jira_url or not settings.jira_email or not settings.jira_api_token:
            raise ValueError("Jira credentials not configured")
        from atlassian import Jira

        self._client = Jira(
            url=settings.jira_url,
            username=settings.jira_email,
            password=settings.jira_api_token,
            cloud=True,
        )

    def get_ticket(self, key: str) -> JiraTicket:
        issue = self._client.issue(key)
        fields = issue.get("fields", {})
        description = fields.get("description") or ""
        if isinstance(description, dict):
            description = _adf_to_text(description)
        acceptance_criteria = extract_acceptance_criteria(fields, description=str(description))
        return JiraTicket(
            key=key,
            summary=fields.get("summary", key),
            description=str(description),
            status=fields.get("status", {}).get("name", "Unknown"),
            issue_type=fields.get("issuetype", {}).get("name", "Task"),
            url=f"{get_settings().jira_url.rstrip('/')}/browse/{key}",
            assignee=(fields.get("assignee") or {}).get("displayName"),
            acceptance_criteria=acceptance_criteria,
        )

    def create_issue(
        self,
        *,
        project_key: str,
        summary: str,
        description: str = "",
        issue_type: str = "Story",
        acceptance_criteria: str = "",
    ) -> JiraTicket:
        body = description
        if acceptance_criteria.strip():
            body = f"{description}\n\nAcceptance criteria:\n{acceptance_criteria}".strip()
        fields: dict[str, Any] = {
            "project": {"key": project_key},
            "summary": summary,
            "description": body,
            "issuetype": {"name": issue_type.title() if issue_type.islower() else issue_type},
        }
        settings = get_settings()
        if settings.jira_ac_field and acceptance_criteria.strip():
            fields[settings.jira_ac_field] = acceptance_criteria
        result = self._client.create_issue(fields=fields)
        key = result["key"]
        return JiraTicket(
            key=key,
            summary=summary,
            description=body,
            status="To Do",
            issue_type=issue_type,
            url=f"{get_settings().jira_url.rstrip('/')}/browse/{key}",
            acceptance_criteria=acceptance_criteria,
        )

    def add_comment(self, key: str, body: str) -> None:
        safe = redact_secrets(body)
        self._client.issue_add_comment(key, safe)
        log.info("Jira comment added to %s", key)

    def transition(self, key: str, status: str) -> None:
        transitions = self._client.get_issue_transitions(key) or []
        for transition in transitions:
            if transition.get("name", "").lower() == status.lower():
                self._client.set_issue_status_by_transition_id(key, transition["id"])
                log.info("Jira %s transitioned to %s", key, status)
                return
        available = [t.get("name", "") for t in transitions]
        log.error(
            "No Jira transition named %r for %s (available: %s) — ticket status unchanged",
            status,
            key,
            available,
        )


_GITHUB_SLUG_RE = re.compile(r"github\.com[:/](?P<owner>[^/]+)/(?P<repo>[^/.]+)")


def origin_repo_slug(workspace: Path) -> str:
    """Return owner/repo parsed from git remote origin, else settings.github_repo."""
    proc = git_run(["remote", "get-url", "origin"], cwd=workspace)
    if proc.returncode != 0:
        return get_settings().github_repo
    match = _GITHUB_SLUG_RE.search((proc.stdout or proc.stderr).strip())
    if not match:
        return get_settings().github_repo
    return f"{match.group('owner')}/{match.group('repo')}"


class PyGithubClient(GitHubClient):
    def __init__(self, *, repo_slug: str | None = None) -> None:
        settings = get_settings()
        if not settings.github_token:
            raise ValueError("GitHub credentials not configured")
        slug = repo_slug or settings.github_repo
        if not slug:
            raise ValueError("GitHub repo not configured")
        from github import Auth, Github

        auth = Auth.Token(settings.github_token)
        self._gh = Github(auth=auth)
        owner, repo = slug.split("/", 1)
        self._repo = self._gh.get_repo(f"{owner}/{repo}")

    def create_pull_request(
        self,
        *,
        title: str,
        body: str,
        head: str,
        base: str = "main",
    ) -> str:
        pr = self._repo.create_pull(
            title=redact_secrets(title), body=redact_secrets(body), head=head, base=base
        )
        log.info("GitHub PR created: %s", pr.html_url)
        return pr.html_url


def get_jira_client() -> JiraClient:
    from sprint_crew.integrations.mocks import MockJiraClient

    settings = get_settings()
    if settings.use_mock_integrations:
        return MockJiraClient()
    return AtlassianJiraClient()


def get_github_client(*, repo_slug: str | None = None) -> GitHubClient:
    from sprint_crew.integrations.mocks import MockGitHubClient

    settings = get_settings()
    if settings.use_mock_integrations:
        return MockGitHubClient()
    return PyGithubClient(repo_slug=repo_slug)


def git_run(
    argv: list[str], *, cwd: Path, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *argv],
        cwd=cwd,
        capture_output=True,
        text=True,
        env=env or os.environ.copy(),
        check=False,
    )


def default_git_env() -> dict[str, str]:
    return {
        **os.environ,
        "GIT_AUTHOR_NAME": "sprint-crew",
        "GIT_AUTHOR_EMAIL": "sprint-crew@local",
        "GIT_COMMITTER_NAME": "sprint-crew",
        "GIT_COMMITTER_EMAIL": "sprint-crew@local",
    }
