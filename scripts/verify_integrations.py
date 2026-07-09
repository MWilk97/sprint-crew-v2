#!/usr/bin/env python3
"""Quick smoke for sandbox Jira + GitHub credentials (no vLLM)."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from sprint_crew.config import get_settings  # noqa: E402
from sprint_crew.integrations.jira_client import get_github_client, get_jira_client  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--skip-github",
        action="store_true",
        help="Only verify Jira operations",
    )
    args = parser.parse_args()

    settings = get_settings()
    if settings.use_mock_integrations:
        print("FAIL: USE_MOCK_INTEGRATIONS=true — set false and configure credentials")
        return 1

    jira = get_jira_client()
    summary = "[sprint-crew-test] integration verify"
    ticket = jira.create_issue(
        project_key=settings.jira_project_key,
        summary=summary,
        description="Automated integration verification.",
        acceptance_criteria="pytest -q passes",
    )
    print(f"OK  Jira create_issue -> {ticket.key}")

    fetched = jira.get_ticket(ticket.key)
    if fetched.summary != summary:
        print(f"FAIL get_ticket summary mismatch: {fetched.summary!r}")
        return 1
    print(f"OK  Jira get_ticket -> {fetched.key}")
    if fetched.acceptance_criteria:
        print(f"OK  acceptance_criteria -> {fetched.acceptance_criteria[:80]!r}")

    jira.add_comment(ticket.key, "sprint-crew verify_integrations smoke comment")
    print("OK  Jira add_comment")

    jira.transition(ticket.key, settings.jira_review_transition)
    print(f"OK  Jira transition -> {settings.jira_review_transition}")

    if not args.skip_github:
        if not settings.github_token or not settings.github_repo:
            print("FAIL: GITHUB_TOKEN / GITHUB_REPO not configured")
            return 1
        github = get_github_client()
        branch = f"sprint-crew-verify-{ticket.key.lower().replace('-', '')}"
        try:
            pr_url = github.create_pull_request(
                title=f"{ticket.key}: integration verify",
                body="Automated verify_integrations — close if opened by mistake.",
                head=branch,
            )
            print(f"OK  GitHub create_pull_request -> {pr_url}")
            print("NOTE: close PR if opened unintentionally.")
        except Exception as exc:
            err = str(exc).lower()
            if "invalid" in err and "head" in err:
                print("OK  GitHub API auth (branch missing on remote — expected for smoke)")
            else:
                print(f"FAIL GitHub create_pull_request: {exc}")
                return 1

    print("PASS: integrations verified")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
