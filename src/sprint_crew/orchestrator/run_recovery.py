"""Startup reconciliation for runs the previous process did not finish (roadmap M5).

Runs live in ``asyncio`` tasks, so a restart kills them without touching their rows. A
``running`` backlog run, sprint session, or console session therefore lied forever — a UI
polling it would spin on a run that no longer exists. Nothing resumes here: the sweep only
makes the stored state honest.

``pending`` counts as interrupted too. Since M5 a queued run is a live task parked on the
registry's admission semaphore, so a ``pending`` row after a restart means that task is gone.
"""

from __future__ import annotations

import logging

from sprint_crew.orchestrator.backlog import backlog_store
from sprint_crew.orchestrator.console_store import console_store
from sprint_crew.orchestrator.session import session_store
from sprint_crew.schemas.console import ConsoleSessionStatus
from sprint_crew.schemas.session import BacklogRunStatus, SessionStatus

logger = logging.getLogger(__name__)

INTERRUPTED_ERROR = "interrupted by restart"

_INTERRUPTED_RUN_STATUSES = frozenset({BacklogRunStatus.PENDING, BacklogRunStatus.RUNNING})
_INTERRUPTED_CONSOLE_STATUSES = frozenset(
    {ConsoleSessionStatus.QUEUED, ConsoleSessionStatus.RUNNING}
)


def sweep_interrupted_runs() -> dict[str, list[str]]:
    """Mark every in-flight row as failed. Returns the swept ids per kind, for logging."""
    swept: dict[str, list[str]] = {
        "backlog_runs": [],
        "sprint_sessions": [],
        "console_sessions": [],
    }

    runs = backlog_store()
    for run in runs.list_all():
        if run.status not in _INTERRUPTED_RUN_STATUSES:
            continue
        runs.save(
            run.model_copy(
                update={"status": BacklogRunStatus.FAILED, "error": run.error or INTERRUPTED_ERROR}
            )
        )
        swept["backlog_runs"].append(run.run_id)

    sessions = session_store()
    for session in sessions.list_all():
        if session.status is not SessionStatus.RUNNING:
            continue
        sessions.save(
            session.model_copy(
                update={
                    "status": SessionStatus.FAILED,
                    "error": session.error or INTERRUPTED_ERROR,
                }
            )
        )
        swept["sprint_sessions"].append(session.session_id)

    # Swept explicitly rather than left to mirror from the backlog run: a session cancelled
    # or interrupted while queued may have no sprint_ref to mirror from at all.
    console = console_store()
    for console_session in console.list_all():
        if console_session.status not in _INTERRUPTED_CONSOLE_STATUSES:
            continue
        console.save(
            console_session.model_copy(
                update={
                    "status": ConsoleSessionStatus.FAILED,
                    "error": console_session.error or INTERRUPTED_ERROR,
                    "queue_position": None,
                }
            )
        )
        swept["console_sessions"].append(console_session.session_id)

    total = sum(len(v) for v in swept.values())
    if total:
        logger.info("startup: marked %d interrupted row(s) as failed: %s", total, swept)
    return swept
