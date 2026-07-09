from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest

from sprint_crew.config import Settings, get_settings
from sprint_crew.orchestrator.backlog import create_jira_tickets, run_backlog
from sprint_crew.schemas.backlog import BacklogIssueType, BacklogPlan, BacklogStory, ProductBrief
from sprint_crew.schemas.session import BacklogRunStatus, SprintSession
from tests.helpers.batch_cycle_fakes import awaiting_session, fake_prepare_workspace
from tests.integration_live.conftest import TEST_SUMMARY_PREFIX


@pytest.mark.integration_live
def test_create_jira_tickets_live(live_jira, integration_live_env: Settings) -> None:
    plan = BacklogPlan(
        product_brief=ProductBrief(title="Greeter", summary="Add hello function"),
        stories=[
            BacklogStory(
                key="STORY-1",
                summary=f"{TEST_SUMMARY_PREFIX} backlog story",
                description="Implement hello() returning 'hello'.",
                issue_type=BacklogIssueType.STORY,
                acceptance_criteria="pytest -q passes",
            ),
        ],
        recommended_first="STORY-1",
    )
    tickets = create_jira_tickets(plan)
    assert "STORY-1" in tickets
    jira_key = tickets["STORY-1"].key
    fetched = live_jira.get_ticket(jira_key)
    assert TEST_SUMMARY_PREFIX in fetched.summary
    assert "pytest -q" in fetched.acceptance_criteria


@pytest.mark.integration_live
@pytest.mark.asyncio
async def test_run_backlog_creates_jira_without_vllm(
    live_jira,
    integration_live_env: Settings,
    tmp_path: Path,
) -> None:
    plan = BacklogPlan(
        product_brief=ProductBrief(title="Greeter", summary="Backlog orchestration"),
        stories=[
            BacklogStory(
                key="STORY-1",
                summary=f"{TEST_SUMMARY_PREFIX} backlog run",
                description="Backlog run integration test without vLLM cycle.",
                issue_type=BacklogIssueType.STORY,
                acceptance_criteria="pytest -q",
            ),
        ],
        recommended_first="STORY-1",
    )

    def fake_prepare(_session_id: str, *, repo_url: str | None = None) -> Path:
        return fake_prepare_workspace(tmp_path)(_session_id, repo_url=repo_url)

    async def fake_create_and_run_cycle(**kwargs) -> SprintSession:
        ticket = kwargs["ticket"]
        workspace = kwargs["workspace"]
        session_id = kwargs["session_id"]
        return awaiting_session(session_id, ticket, workspace)

    with (
        patch("sprint_crew.orchestrator.batch_cycle.prepare_workspace", side_effect=fake_prepare),
        patch(
            "sprint_crew.orchestrator.batch_cycle.create_and_run_cycle",
            side_effect=fake_create_and_run_cycle,
        ),
        patch("sprint_crew.orchestrator.batch_cycle.maybe_index_workspace", return_value=None),
        patch("sprint_crew.orchestrator.batch_cycle._stop_all_lanes", new=AsyncMock()),
    ):
        get_settings.cache_clear()
        run = await run_backlog(
            run_id=str(uuid4()),
            plan=plan,
            user_prompt="Add hello()",
            use_real_ship=False,
        )

    assert run.status == BacklogRunStatus.COMPLETED
    assert len(run.session_ids) == 1
    assert run.session_ids[0]
