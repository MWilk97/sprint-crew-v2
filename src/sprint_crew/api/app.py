from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from uuid import uuid4

from fastapi import APIRouter, BackgroundTasks, Depends, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from sprint_crew.agents.scrum_master import run_scrum_master
from sprint_crew.api.auth import require_token
from sprint_crew.api.console import router as console_router
from sprint_crew.api.console import run_console_reaper
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

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    # Sweep stale terminal console sessions (and their workspaces) once at startup;
    # the per-completion sweep in console.py keeps it current afterward.
    try:
        reaped = run_console_reaper()
        if reaped:
            logger.info("startup: reaped %d stale console session(s)", len(reaped))
    except Exception:
        logger.exception("startup console reaper failed")
    yield


app = FastAPI(title="Sprint Crew API", version="0.1.0", lifespan=lifespan)

# CORS for the browser console (separate repo/origin, ADR 0011). Origins come from
# Settings.CONSOLE_CORS_ORIGINS; default "*" suits local dev. Auth is a shared bearer
# (CONSOLE_API_TOKEN); credentials stay off — Bearer rides in Authorization, not cookies.
_settings = get_settings()
app.add_middleware(
    CORSMiddleware,
    allow_origins=_settings.console_cors_origin_list,
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.include_router(console_router)

sprint_router = APIRouter(prefix="/sprint", tags=["sprint"], dependencies=[Depends(require_token)])


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


@app.get("/")
async def root() -> dict[str, str]:
    """Friendly landing for browsers / FE base-URL checks (API has no HTML UI)."""
    return {
        "service": "sprint-crew",
        "docs": "/docs",
        "health": "/health",
        "console": "/v1/console/sessions",
    }


@app.get("/health")
async def health() -> dict[str, object]:
    return {"status": "ok", "lanes": await asyncio.to_thread(lane_health)}


async def start_from_prompt_run(
    prompt: str,
    repo_url: str | None,
    background_tasks: BackgroundTasks,
    console_session_id: str | None = None,
) -> str:
    """From-prompt orchestration shared by /sprint/from-prompt and console code-mode start."""
    settings = get_settings()
    run_id = str(uuid4())
    workspace_id = f"backlog-{run_id}"
    # to_thread: clone, indexing, and context enrichment are blocking I/O; keep them off
    # the event loop so /health and any live SSE stream stay responsive during start (M3).
    workspace = await asyncio.to_thread(prepare_workspace, workspace_id, repo_url=repo_url)
    await asyncio.to_thread(
        maybe_index_workspace,
        workspace,
        workspace_id,
        prompt=prompt,
    )
    repo_context = await asyncio.to_thread(enrich_repo_context, workspace, workspace_id, prompt)

    work_lane = Role.WORK
    await ensure_lane(work_lane)
    try:
        plan = await run_scrum_master(
            user_prompt=prompt,
            repo_context=repo_context,
            role=work_lane,
        )
    finally:
        await stop_lane(work_lane)

    BacklogRunStore(get_settings().session_db).save(
        BacklogRun(
            run_id=run_id,
            status=BacklogRunStatus.PENDING,
            user_prompt=prompt,
            repo_url=repo_url,
        )
    )

    background_tasks.add_task(
        run_backlog_batched,
        run_id=run_id,
        plan=plan,
        user_prompt=prompt,
        repo_url=repo_url,
        use_real_ship=not settings.use_mock_integrations,
        console_session_id=console_session_id,
    )
    return run_id


@sprint_router.post("/from-prompt", response_model=BacklogRunCreatedResponse)
async def sprint_from_prompt(
    body: FromPromptRequest, background_tasks: BackgroundTasks
) -> BacklogRunCreatedResponse:
    run_id = await start_from_prompt_run(body.prompt, body.repo_url, background_tasks)
    return BacklogRunCreatedResponse(run_id=run_id)


@sprint_router.post("/from-ticket", response_model=SessionCreatedResponse)
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


@sprint_router.get("/backlog/{run_id}", response_model=BacklogRun)
async def get_backlog(run_id: str) -> BacklogRun:
    run = get_backlog_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Backlog run not found")
    return run


@sprint_router.get("/session/{session_id}", response_model=SprintSession)
async def get_sprint_session(session_id: str) -> SprintSession:
    session = get_session(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found")
    return session


@sprint_router.post("/session/{session_id}/approve", response_model=SprintSession)
async def approve_sprint_session(session_id: str) -> SprintSession:
    try:
        return approve_session(session_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


app.include_router(sprint_router)
