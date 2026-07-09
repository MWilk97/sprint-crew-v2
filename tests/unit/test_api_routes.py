"""FastAPI routes against SessionStore / BacklogRunStore (no handler mocks)."""

from __future__ import annotations

from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient

from sprint_crew.api.app import app
from sprint_crew.orchestrator.backlog import BacklogRunStore
from sprint_crew.orchestrator.session import SessionStore
from sprint_crew.schemas.change import ReviewOutcome
from sprint_crew.schemas.session import BacklogRun, BacklogRunStatus, SessionStatus, SprintSession
from sprint_crew.schemas.ticket import JiraTicket


@pytest.mark.asyncio
async def test_health_ok() -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


@pytest.mark.asyncio
async def test_get_sprint_session_found(api_db, sample_ticket: JiraTicket) -> None:
    session = SprintSession(
        session_id="session-abc",
        status=SessionStatus.RUNNING,
        ticket_key=sample_ticket.key,
        workspace_root="/tmp/ws",
        selected_ticket=sample_ticket,
    )
    SessionStore(api_db).save(session)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/sprint/session/session-abc")
    assert resp.status_code == 200
    assert resp.json()["session_id"] == "session-abc"


@pytest.mark.asyncio
async def test_get_sprint_session_not_found(api_db) -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/sprint/session/missing")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_get_backlog_run_found(api_db) -> None:
    run = BacklogRun(
        run_id="run-abc",
        status=BacklogRunStatus.COMPLETED,
        user_prompt="Add hello()",
        session_ids=["s1"],
    )
    BacklogRunStore(api_db).save(run)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/sprint/backlog/run-abc")
    assert resp.status_code == 200
    assert resp.json()["run_id"] == "run-abc"


@pytest.mark.asyncio
async def test_get_backlog_run_not_found(api_db) -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/sprint/backlog/missing")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_approve_session_success(api_db, sample_ticket: JiraTicket) -> None:
    session = SprintSession(
        session_id=f"approve-{uuid4().hex[:8]}",
        status=SessionStatus.AWAITING_HUMAN,
        ticket_key=sample_ticket.key,
        workspace_root=str(api_db.parent),
        selected_ticket=sample_ticket,
        review_outcome=ReviewOutcome(
            ticket_key=sample_ticket.key,
            passed=True,
            summary="ok",
            tests_passed=True,
        ),
    )
    SessionStore(api_db).save(session)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(f"/sprint/session/{session.session_id}/approve")
    assert resp.status_code == 200
    assert resp.json()["status"] == SessionStatus.APPROVED.value


@pytest.mark.asyncio
async def test_approve_session_bad_status(api_db, sample_ticket: JiraTicket) -> None:
    session = SprintSession(
        session_id="session-bad",
        status=SessionStatus.RUNNING,
        ticket_key=sample_ticket.key,
        workspace_root=str(api_db.parent),
        selected_ticket=sample_ticket,
    )
    SessionStore(api_db).save(session)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post("/sprint/session/session-bad/approve")
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_approve_session_not_found(api_db) -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post("/sprint/session/missing/approve")
    assert resp.status_code == 404
