from __future__ import annotations

import asyncio
import logging
import shutil
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from uuid import uuid4

from fastapi import APIRouter, Depends, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware

from sprint_crew.api.auth import require_token
from sprint_crew.api.console import router as console_router
from sprint_crew.api.console import run_console_reaper
from sprint_crew.config import get_settings
from sprint_crew.graph.lanes import lane_health, stop_all_lanes
from sprint_crew.integrations.jira_client import get_jira_client
from sprint_crew.orchestrator.backlog import BacklogRunStore, get_backlog_run
from sprint_crew.orchestrator.console_store import sweep_interrupted_workspace_prep
from sprint_crew.orchestrator.event_log import event_log
from sprint_crew.orchestrator.prompt_run import run_from_prompt
from sprint_crew.orchestrator.run_recovery import sweep_interrupted_runs
from sprint_crew.orchestrator.run_registry import run_registry
from sprint_crew.orchestrator.session import (
    SessionStore,
    approve_session,
    create_and_run_cycle,
    get_session,
    prepare_workspace,
)
from sprint_crew.schemas.backlog import BacklogPlan
from sprint_crew.schemas.session import (
    BacklogRun,
    BacklogRunStatus,
    SessionStatus,
    SprintSession,
    agent_event,
)
from sprint_crew.schemas.sprint import (
    BacklogRunCreatedResponse,
    FromPromptRequest,
    FromTicketRequest,
    SessionCreatedResponse,
)
from sprint_crew.vector.indexer import sweep_orphan_collections

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    # Runs live in asyncio tasks, so a restart kills them without touching their rows.
    # Reconcile before serving: a `running` row that no task backs is a lie the UI polls.
    try:
        sweep_interrupted_runs()
    except Exception:
        logger.exception("startup interrupted-run sweep failed")
    # Workspace prep runs in an asyncio task too, so it dies with the process the same way.
    try:
        sweep_interrupted_workspace_prep()
    except Exception:
        logger.exception("startup workspace-prep sweep failed")
    # Sweep stale terminal console sessions (and their workspaces) once at startup;
    # the per-completion sweep in console.py keeps it current afterward.
    try:
        reaped = run_console_reaper()
        if reaped:
            logger.info("startup: reaped %d stale console session(s)", len(reaped))
    except Exception:
        logger.exception("startup console reaper failed")
    # Collections from before the M8 re-key are unreferenced and nothing else would ever
    # collect them. to_thread because this one talks to Qdrant over the network — a slow
    # or unreachable service must not hold the event loop before the app can serve.
    try:
        await asyncio.to_thread(sweep_orphan_collections)
    except Exception:
        logger.exception("startup orphan-collection sweep failed")
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
    return {
        "status": "ok",
        "lanes": await asyncio.to_thread(lane_health),
        "workspaces": await asyncio.to_thread(workspace_usage),
    }


def workspace_usage() -> dict[str, object]:
    """Clone count and free disk. Every console session is a checkout now, so disk is the
    constraint worth watching; ``disk_usage`` is O(1) where walking the tree would not be."""
    base = get_settings().workspace_base
    if not base.exists():
        return {"count": 0}
    usage = shutil.disk_usage(base)
    return {
        "count": sum(1 for child in base.iterdir() if child.is_dir()),
        "disk_free_bytes": usage.free,
        "disk_total_bytes": usage.total,
    }


async def start_from_prompt_run(
    prompt: str,
    repo_url: str | None,
    console_session_id: str | None = None,
    plan: BacklogPlan | None = None,
) -> str:
    """Queue a from-prompt run and return its id. Returns in milliseconds.

    Shared by ``POST /sprint/from-prompt`` and console code-mode start. Everything that
    actually costs time — clone, index, enrichment, lane load, ScrumMaster, then the batch —
    happens in the registry-owned task (``orchestrator/prompt_run.py``) and reports progress
    on the event stream.
    """
    settings = get_settings()
    run_id = str(uuid4())
    store = BacklogRunStore(settings.session_db)
    store.save(
        BacklogRun(
            run_id=run_id,
            status=BacklogRunStatus.PENDING,
            user_prompt=prompt,
            repo_url=repo_url,
        )
    )
    use_real_ship = not settings.use_mock_integrations

    async def _body() -> None:
        await run_from_prompt(
            run_id=run_id,
            prompt=prompt,
            repo_url=repo_url,
            use_real_ship=use_real_ship,
            console_session_id=console_session_id,
            plan=plan,
        )

    async def _on_admit() -> None:
        run = await asyncio.to_thread(store.load, run_id)
        if run is not None:
            await asyncio.to_thread(
                store.save, run.model_copy(update={"status": BacklogRunStatus.RUNNING})
            )

    registry = run_registry()
    registry.submit(
        run_id,
        _body,
        console_session_id=console_session_id,
        on_admit=_on_admit,
        # Repeated from the run's own finally block, which a hard cancel can interrupt
        # mid-await. Idempotent, and a lane left loaded wedges the GPU for everything else.
        teardown=stop_all_lanes,
    )
    _emit_run_queued(run_id, console_session_id, registry.position(run_id))
    return run_id


def _emit_run_queued(run_id: str, console_session_id: str | None, position: int | None) -> None:
    if not console_session_id:
        return
    event_log().append_many(
        console_session_id,
        None,
        [
            agent_event(
                "orchestrator",
                "run_queued",
                f"Run queued behind {position} run(s)" if position else "Run starting",
                run_id=run_id,
                queue_position=position or None,
            )
        ],
    )


@sprint_router.post("/from-prompt", response_model=BacklogRunCreatedResponse, status_code=202)
async def sprint_from_prompt(body: FromPromptRequest) -> BacklogRunCreatedResponse:
    run_id = await start_from_prompt_run(body.prompt, body.repo_url)
    return BacklogRunCreatedResponse(run_id=run_id)


@sprint_router.post("/from-ticket", response_model=SessionCreatedResponse, status_code=202)
async def sprint_from_ticket(body: FromTicketRequest) -> SessionCreatedResponse:
    settings = get_settings()
    jira = get_jira_client()
    ticket = jira.get_ticket(body.ticket_key)

    session_id = str(uuid4())
    workspace = await asyncio.to_thread(prepare_workspace, session_id, repo_url=body.repo_url)
    pending = SprintSession(
        session_id=session_id,
        status=SessionStatus.PENDING,
        ticket_key=ticket.key,
        workspace_root=str(workspace),
        selected_ticket=ticket,
    )
    SessionStore(settings.session_db).save(pending)
    use_real_ship = not settings.use_mock_integrations

    async def _body() -> None:
        await create_and_run_cycle(
            ticket=ticket,
            workspace=workspace,
            session_id=session_id,
            use_real_ship=use_real_ship,
        )

    run_registry().submit(session_id, _body, teardown=stop_all_lanes)
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
