"""FastAPI routes requiring live Jira or backlog orchestration."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from sprint_crew.api.app import app
from sprint_crew.config import Settings
from sprint_crew.orchestrator.backlog import BacklogRunStore
from sprint_crew.schemas.backlog import BacklogIssueType, BacklogPlan, BacklogStory, ProductBrief
from sprint_crew.schemas.session import BacklogRunStatus, SessionStatus, SprintSession
from tests.integration_live.conftest import TEST_SUMMARY_PREFIX


@pytest.mark.integration_live
@pytest.mark.asyncio
async def test_from_ticket_fetches_real_jira(
    integration_live_env: Settings,
    live_jira,
    api_db,
) -> None:
    """POST /sprint/from-ticket uses real Jira; cycle is not started (background mocked)."""
    ticket = live_jira.create_issue(
        project_key=integration_live_env.jira_project_key,
        summary=f"{TEST_SUMMARY_PREFIX} from-ticket route",
        description="Route smoke — no agent cycle.",
        acceptance_criteria="pytest -q",
    )

    with patch(
        "sprint_crew.api.app.create_and_run_cycle",
        new=AsyncMock(
            return_value=SprintSession(
                session_id="route-smoke",
                status=SessionStatus.PENDING,
                ticket_key=ticket.key,
                workspace_root=str(api_db.parent),
                selected_ticket=ticket,
            )
        ),
    ):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post("/sprint/from-ticket", json={"ticket_key": ticket.key})

    assert resp.status_code == 200
    assert "session_id" in resp.json()


@pytest.mark.integration_live
@pytest.mark.asyncio
async def test_from_prompt_route_persists_backlog_run(
    api_db,
    integration_live_env: Settings,
) -> None:
    """POST /sprint/from-prompt stores BacklogRun; ScrumMaster + backlog task mocked (no GPU)."""
    plan = BacklogPlan(
        product_brief=ProductBrief(title="Greeter", summary="Add hello"),
        stories=[
            BacklogStory(
                key="STORY-1",
                summary=f"{TEST_SUMMARY_PREFIX} from-prompt route",
                description="Route smoke — backlog task not executed.",
                issue_type=BacklogIssueType.STORY,
                acceptance_criteria="pytest -q",
            ),
        ],
        recommended_first="STORY-1",
    )

    async def fake_run_backlog_batched(**kwargs) -> None:
        store = BacklogRunStore(api_db)
        run = store.load(kwargs["run_id"])
        if run is not None:
            store.save(run.model_copy(update={"status": BacklogRunStatus.COMPLETED}))

    with (
        patch("sprint_crew.api.app.run_scrum_master", new=AsyncMock(return_value=plan)),
        patch("sprint_crew.api.app.ensure_lane", new=AsyncMock()),
        patch("sprint_crew.api.app.stop_lane", new=AsyncMock()),
        patch(
            "sprint_crew.api.app.run_backlog_batched",
            new=AsyncMock(side_effect=fake_run_backlog_batched),
        ),
        patch("sprint_crew.api.app.prepare_workspace", return_value=api_db.parent / "ws"),
    ):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post(
                "/sprint/from-prompt",
                json={"prompt": "Add hello() to greeter.py with pytest."},
            )

    assert resp.status_code == 200
    run_id = resp.json()["run_id"]
    stored = BacklogRunStore(api_db).load(run_id)
    assert stored is not None
    assert stored.user_prompt == "Add hello() to greeter.py with pytest."
