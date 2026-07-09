from __future__ import annotations

from sprint_crew.integrations.jira_client import GitHubClient, JiraClient, redact_secrets
from sprint_crew.schemas.ticket import JiraTicket


class MockJiraClient(JiraClient):
    def __init__(self) -> None:
        self.comments: dict[str, list[str]] = {}
        self.transitions: dict[str, list[str]] = {}
        self._counter = 0

    def get_ticket(self, key: str) -> JiraTicket:
        return JiraTicket(
            key=key,
            summary=f"Mock ticket {key}",
            description="Mock description",
            status="To Do",
            issue_type="Story",
            url=f"https://jira.example/browse/{key}",
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
        self._counter += 1
        key = f"{project_key}-{self._counter}"
        body = description
        if acceptance_criteria.strip():
            body = f"{description}\n\nAcceptance criteria:\n{acceptance_criteria}".strip()
        return JiraTicket(
            key=key,
            summary=summary,
            description=body,
            status="To Do",
            issue_type=issue_type,
            url=f"https://jira.example/browse/{key}",
            acceptance_criteria=acceptance_criteria,
        )

    def add_comment(self, key: str, body: str) -> None:
        self.comments.setdefault(key, []).append(body)

    def transition(self, key: str, status: str) -> None:
        self.transitions.setdefault(key, []).append(status)


class MockGitHubClient(GitHubClient):
    def __init__(self) -> None:
        self.pull_requests: list[dict[str, str]] = []

    def create_pull_request(
        self,
        *,
        title: str,
        body: str,
        head: str,
        base: str = "main",
    ) -> str:
        url = f"https://github.example/mock/pull/{len(self.pull_requests) + 1}"
        self.pull_requests.append(
            {
                "title": redact_secrets(title),
                "body": redact_secrets(body),
                "head": head,
                "base": base,
                "url": url,
            }
        )
        return url
