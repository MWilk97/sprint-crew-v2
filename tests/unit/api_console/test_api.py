"""FastAPI routes (against real SessionStore/BacklogRunStore) + console schema validation."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
from httpx import AsyncClient
from pydantic import ValidationError

from sprint_crew.api.app import app
from sprint_crew.orchestrator.backlog import BacklogRunStore
from sprint_crew.orchestrator.prompt_run import run_from_prompt
from sprint_crew.orchestrator.session import SessionStore
from sprint_crew.schemas.change import ReviewOutcome
from sprint_crew.schemas.console import (
    ClarifyAnswer,
    ClarifyQuestion,
    ClarifyRequest,
    ClarifySuggestion,
    ConsoleMode,
    ConsoleSession,
    ConsoleSessionStatus,
    CreateConsoleSessionRequest,
    SprintRunRef,
)
from sprint_crew.schemas.session import BacklogRun, BacklogRunStatus, SessionStatus, SprintSession
from sprint_crew.schemas.sprint import FromPromptRequest, FromTicketRequest
from sprint_crew.schemas.ticket import JiraTicket


@pytest.mark.asyncio
async def test_health_ok(api_client: AsyncClient) -> None:
    resp = await api_client.get("/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


@pytest.mark.asyncio
async def test_get_sprint_session_found(
    api_client: AsyncClient, api_db, sample_ticket: JiraTicket
) -> None:
    session = SprintSession(
        session_id="session-abc",
        status=SessionStatus.RUNNING,
        ticket_key=sample_ticket.key,
        workspace_root="/tmp/ws",
        selected_ticket=sample_ticket,
    )
    SessionStore(api_db).save(session)

    resp = await api_client.get("/sprint/session/session-abc")
    assert resp.status_code == 200
    assert resp.json()["session_id"] == "session-abc"


@pytest.mark.asyncio
async def test_get_sprint_session_not_found(api_client: AsyncClient, api_db) -> None:
    resp = await api_client.get("/sprint/session/missing")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_get_backlog_run_found(api_client: AsyncClient, api_db) -> None:
    run = BacklogRun(
        run_id="run-abc",
        status=BacklogRunStatus.COMPLETED,
        user_prompt="Add hello()",
        session_ids=["s1"],
    )
    BacklogRunStore(api_db).save(run)

    resp = await api_client.get("/sprint/backlog/run-abc")
    assert resp.status_code == 200
    assert resp.json()["run_id"] == "run-abc"


@pytest.mark.asyncio
async def test_get_backlog_run_not_found(api_client: AsyncClient, api_db) -> None:
    resp = await api_client.get("/sprint/backlog/missing")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_approve_session_success(
    api_client: AsyncClient, api_db, sample_ticket: JiraTicket
) -> None:
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

    resp = await api_client.post(f"/sprint/session/{session.session_id}/approve")
    assert resp.status_code == 200
    assert resp.json()["status"] == SessionStatus.APPROVED.value


@pytest.mark.asyncio
async def test_approve_session_bad_status(
    api_client: AsyncClient, api_db, sample_ticket: JiraTicket
) -> None:
    session = SprintSession(
        session_id="session-bad",
        status=SessionStatus.RUNNING,
        ticket_key=sample_ticket.key,
        workspace_root=str(api_db.parent),
        selected_ticket=sample_ticket,
    )
    SessionStore(api_db).save(session)

    resp = await api_client.post("/sprint/session/session-bad/approve")
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_approve_session_not_found(api_client: AsyncClient, api_db) -> None:
    resp = await api_client.post("/sprint/session/missing/approve")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_from_prompt_planning_stops_work_lane_when_scrum_master_fails(tmp_path) -> None:
    """Regression: ensure_lane/stop_lane must bracket run_scrum_master with try/finally —
    otherwise an exception mid-call leaves the Work lane loaded (GX10 unified-memory risk,
    see AGENTS.md 4.1: never keep 2+ vLLM lanes loaded at once).

    Planning moved out of the request handler into the registry-owned run body (M5), so the
    invariant is asserted there; the handler no longer awaits any of this.
    """
    stop_mock = AsyncMock()
    with (
        patch("sprint_crew.orchestrator.prompt_run.prepare_workspace", return_value=tmp_path),
        patch("sprint_crew.orchestrator.prompt_run.maybe_index_shared"),
        patch("sprint_crew.orchestrator.prompt_run.enrich_repo_context", return_value=""),
        patch("sprint_crew.orchestrator.prompt_run.ensure_lane", new=AsyncMock()),
        patch("sprint_crew.orchestrator.prompt_run.stop_lane", new=stop_mock),
        patch(
            "sprint_crew.orchestrator.prompt_run.run_scrum_master",
            new=AsyncMock(side_effect=RuntimeError("boom")),
        ),
    ):
        with pytest.raises(RuntimeError):
            await run_from_prompt(
                run_id="run-lane",
                prompt="add a feature",
                repo_url=None,
                use_real_ship=False,
            )

    stop_mock.assert_awaited_once()


def _question() -> ClarifyQuestion:
    return ClarifyQuestion(
        question_id="q-scope",
        text="Which part of the repo should change?",
        suggestions=[ClarifySuggestion(suggestion_id="s-api", label="API layer only")],
        allow_custom=True,
    )


def test_create_session_happy_path() -> None:
    req = CreateConsoleSessionRequest(mode=ConsoleMode.CODE, initial_prompt="add health endpoint")
    assert req.mode is ConsoleMode.CODE
    assert req.target_language is None


def test_session_round_trip_with_clarify_and_sprint_ref() -> None:
    session = ConsoleSession(
        session_id="cs-1",
        mode=ConsoleMode.CODE,
        status=ConsoleSessionStatus.RUNNING,
        confirmed=True,
        clarify_questions=[_question()],
        clarify_answers=[ClarifyAnswer(question_id="q-scope", selected_suggestion_id="s-api")],
        sprint_ref=SprintRunRef(backlog_run_id="run-1", sprint_session_ids=["session-a"]),
    )
    restored = ConsoleSession.model_validate(session.model_dump())
    assert restored.status is ConsoleSessionStatus.RUNNING
    assert restored.sprint_ref is not None
    assert restored.sprint_ref.backlog_run_id == "run-1"


def test_clarify_answer_requires_exactly_one() -> None:
    with pytest.raises(ValidationError):
        ClarifyAnswer(question_id="q-scope")
    with pytest.raises(ValidationError):
        ClarifyAnswer(question_id="q-scope", selected_suggestion_id="s-api", custom_text="both")
    answer = ClarifyAnswer(question_id="q-scope", custom_text="only the CLI")
    assert answer.custom_text == "only the CLI"


def test_unknown_fields_rejected() -> None:
    with pytest.raises(ValidationError):
        CreateConsoleSessionRequest(mode=ConsoleMode.PLAN, surprise=True)
    with pytest.raises(ValidationError):
        ClarifyRequest(
            answers=[ClarifyAnswer(question_id="q", custom_text="x")],
            extra_field="nope",
        )


def test_sprint_request_models_reject_unknown_fields() -> None:
    """These four lived inline in api/app.py as bare BaseModel — the only API models in the
    codebase without extra="forbid", so /sprint/* silently accepted typos that
    /v1/console/* rejected.
    """
    with pytest.raises(ValidationError):
        FromPromptRequest(prompt="build it", repo_urls="typo")
    with pytest.raises(ValidationError):
        FromTicketRequest(ticket_key="DEMO-1", branch="nope")


def test_console_routes_registered() -> None:
    paths = set(app.openapi()["paths"])
    expected = {
        "/v1/console/sessions",
        "/v1/console/sessions/{id}",
        "/v1/console/sessions/{id}/messages",
        "/v1/console/sessions/{id}/clarify",
        "/v1/console/sessions/{id}/confirm",
        "/v1/console/sessions/{id}/start",
        "/v1/console/sessions/{id}/cancel",
    }
    assert expected <= paths
