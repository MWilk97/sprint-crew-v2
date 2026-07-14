from __future__ import annotations

import asyncio
from uuid import uuid4

from fastapi import BackgroundTasks, FastAPI, HTTPException
from pydantic import BaseModel, Field

from sprint_crew.agents.scrum_master import run_scrum_master
from sprint_crew.config import Role, get_settings
from sprint_crew.graph.lanes import ensure_lane, lane_health, stop_lane
from sprint_crew.integrations.jira_client import get_jira_client
from sprint_crew.orchestrator.backlog import BacklogRunStore, get_backlog_run
from sprint_crew.orchestrator.batch_cycle import run_backlog_batched
from sprint_crew.orchestrator.repo_context import enrich_repo_context, maybe_index_workspace
from sprint_crew.orchestrator.session import (
    SessionStore,
    approve_session,
    create_and_run_cycle,
    get_session,
    prepare_workspace,
)
from sprint_crew.schemas.session import BacklogRun, BacklogRunStatus, SessionStatus, SprintSession

app = FastAPI(title="Sprint Crew API", version="0.1.0")


class FromPromptRequest(BaseModel):
    prompt: str = Field(..., min_length=1)
    repo_url: str | None = None


class FromTicketRequest(BaseModel):
    ticket_key: str = Field(..., min_length=1)
    repo_url: str | None = None


class SessionCreatedResponse(BaseModel):
    session_id: str


class BacklogRunCreatedResponse(BaseModel):
    run_id: str


@app.get("/health")
async def health() -> dict[str, object]:
    return {"status": "ok", "lanes": lane_health()}


@app.post("/sprint/from-prompt", response_model=BacklogRunCreatedResponse)
async def sprint_from_prompt(
    body: FromPromptRequest, background_tasks: BackgroundTasks
) -> BacklogRunCreatedResponse:
    settings = get_settings()
    run_id = str(uuid4())
    workspace_id = f"backlog-{run_id}"
    workspace = prepare_workspace(workspace_id, repo_url=body.repo_url)
    await asyncio.to_thread(
        maybe_index_workspace,
        workspace,
        workspace_id,
        prompt=body.prompt,
    )
    repo_context = enrich_repo_context(workspace, workspace_id, body.prompt)

    work_lane = Role.WORK
    await ensure_lane(work_lane)
    try:
        plan = await run_scrum_master(
            user_prompt=body.prompt,
            repo_context=repo_context,
            role=work_lane,
        )
    finally:
        await stop_lane(work_lane)

    BacklogRunStore(get_settings().session_db).save(
        BacklogRun(
            run_id=run_id,
            status=BacklogRunStatus.PENDING,
            user_prompt=body.prompt,
            repo_url=body.repo_url,
        )
    )

    background_tasks.add_task(
        run_backlog_batched,
        run_id=run_id,
        plan=plan,
        user_prompt=body.prompt,
        repo_url=body.repo_url,
        use_real_ship=not settings.use_mock_integrations,
    )
    return BacklogRunCreatedResponse(run_id=run_id)


@app.post("/sprint/from-ticket", response_model=SessionCreatedResponse)
async def sprint_from_ticket(
    body: FromTicketRequest, background_tasks: BackgroundTasks
) -> SessionCreatedResponse:
    settings = get_settings()
    jira = get_jira_client()
    ticket = jira.get_ticket(body.ticket_key)

    session_id = str(uuid4())
    workspace = prepare_workspace(session_id, repo_url=body.repo_url)
    pending = SprintSession(
        session_id=session_id,
        status=SessionStatus.PENDING,
        ticket_key=ticket.key,
        workspace_root=str(workspace),
        selected_ticket=ticket,
    )
    SessionStore(settings.session_db).save(pending)

    background_tasks.add_task(
        create_and_run_cycle,
        ticket=ticket,
        workspace=workspace,
        session_id=session_id,
        use_real_ship=not settings.use_mock_integrations,
    )
    return SessionCreatedResponse(session_id=session_id)


@app.get("/sprint/backlog/{run_id}", response_model=BacklogRun)
async def get_backlog(run_id: str) -> BacklogRun:
    run = get_backlog_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Backlog run not found")
    return run


@app.get("/sprint/session/{session_id}", response_model=SprintSession)
async def get_sprint_session(session_id: str) -> SprintSession:
    session = get_session(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found")
    return session


@app.post("/sprint/session/{session_id}/approve", response_model=SprintSession)
async def approve_sprint_session(session_id: str) -> SprintSession:
    try:
        return approve_session(session_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
