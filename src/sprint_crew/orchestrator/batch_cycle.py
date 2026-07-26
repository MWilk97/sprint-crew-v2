from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from uuid import uuid4

from sprint_crew.config import Role
from sprint_crew.graph.lanes import stop_lane
from sprint_crew.orchestrator.backlog import (
    backlog_store,
    create_jira_tickets,
    sort_stories,
)
from sprint_crew.orchestrator.run_registry import RunCancelled, check_cancelled
from sprint_crew.orchestrator.session import (
    create_and_run_cycle,
    prepare_chained_workspace,
    prepare_workspace,
)
from sprint_crew.schemas.backlog import BacklogPlan
from sprint_crew.schemas.session import BacklogRun, BacklogRunStatus, SessionStatus
from sprint_crew.vector.indexer import delete_workspace_index

logger = logging.getLogger(__name__)


async def _stop_all_lanes() -> None:
    for role in Role:
        try:
            await stop_lane(role)
        except Exception:
            logger.warning("Failed to stop lane %s during backlog cleanup", role, exc_info=True)


def _cleanup_vector_indexes(session_ids: list[str], run_id: str) -> None:
    for session_id in session_ids:
        try:
            delete_workspace_index(session_id)
        except Exception:
            logger.warning(
                "Failed to delete vector index for session %s", session_id, exc_info=True
            )
    try:
        delete_workspace_index(f"backlog-{run_id}")
    except Exception:
        logger.warning("Failed to delete vector index for backlog run %s", run_id, exc_info=True)


async def run_backlog_batched(
    *,
    run_id: str,
    plan: BacklogPlan,
    user_prompt: str,
    repo_url: str | None = None,
    use_real_ship: bool = False,
    console_session_id: str | None = None,
) -> BacklogRun:
    store = backlog_store()
    run = BacklogRun(
        run_id=run_id,
        status=BacklogRunStatus.RUNNING,
        user_prompt=user_prompt,
        repo_url=repo_url,
    )
    store.save(run)

    session_ids: list[str] = []
    completed_session_ids: list[str] = []
    failed_ticket_key: str | None = None
    parent_workspace: Path | None = None
    try:
        tickets = create_jira_tickets(plan)
        for story in sort_stories(plan):
            # Cheapest honest place to stop: between stories nothing is half-written, and
            # the previous story's PR is already open and independently reviewable. Checking
            # here rather than after the cycle keeps the just-shipped story's bookkeeping —
            # a Stop must not retract a PR that already exists.
            check_cancelled()
            ticket = tickets[story.key]
            session_id = str(uuid4())
            if parent_workspace is None:
                workspace = prepare_workspace(session_id, repo_url=repo_url)
            else:
                workspace = prepare_chained_workspace(parent_workspace, session_id)

            session_ids.append(session_id)
            session = await create_and_run_cycle(
                ticket=ticket,
                workspace=workspace,
                session_id=session_id,
                user_prompt=user_prompt,
                use_real_ship=use_real_ship,
                backlog_run_id=run_id,
                console_session_id=console_session_id,
            )

            # create_and_run_cycle absorbs a cooperative cancel into a CANCELLED session
            # rather than re-raising, so translate it back — otherwise the failure branch
            # below would read a cancelled story as a crash and mark the run FAILED.
            if session.status == SessionStatus.CANCELLED:
                raise RunCancelled(session.error or "cancelled")

            if session.status == SessionStatus.AWAITING_HUMAN:
                completed_session_ids.append(session_id)
                parent_workspace = workspace
                try:
                    delete_workspace_index(session_id)
                except Exception:
                    logger.warning(
                        "Failed to delete vector index for completed session %s",
                        session_id,
                        exc_info=True,
                    )
                run = run.model_copy(
                    update={
                        "session_ids": session_ids,
                        "completed_session_ids": completed_session_ids,
                    }
                )
                store.save(run)
                continue

            failed_ticket_key = ticket.key
            run = run.model_copy(
                update={
                    "status": BacklogRunStatus.FAILED,
                    "session_ids": session_ids,
                    "completed_session_ids": completed_session_ids,
                    "failed_ticket_key": failed_ticket_key,
                    "error": session.error or f"Sprint batch failed at {ticket.key}",
                }
            )
            store.save(run)
            return run

        run = run.model_copy(
            update={
                "status": BacklogRunStatus.COMPLETED,
                "session_ids": session_ids,
                "completed_session_ids": completed_session_ids,
            }
        )
        store.save(run)
        return run
    except (RunCancelled, asyncio.CancelledError) as exc:
        # Ahead of the generic arm below, which would report a cancel as a failure. Stories
        # that already shipped stay in completed_session_ids — their PRs are open and the
        # user's Stop does not retract them.
        run = run.model_copy(
            update={
                "status": BacklogRunStatus.CANCELLED,
                "session_ids": session_ids,
                "completed_session_ids": completed_session_ids,
                "error": str(exc) or "cancelled",
            }
        )
        store.save(run)
        if isinstance(exc, asyncio.CancelledError):
            raise
        return run
    except Exception as exc:
        run = run.model_copy(
            update={
                "status": BacklogRunStatus.FAILED,
                "session_ids": session_ids,
                "completed_session_ids": completed_session_ids,
                "failed_ticket_key": failed_ticket_key,
                "error": str(exc),
            }
        )
        store.save(run)
        return run
    finally:
        # Best-effort: a hard cancel can interrupt these awaits mid-way, which is why the
        # RunRegistry repeats the lane teardown from its uncancelled wrapper task.
        await _stop_all_lanes()
        _cleanup_vector_indexes(session_ids, run_id)
