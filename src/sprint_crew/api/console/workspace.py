"""The session's own checkout and index, prepared in the background (roadmap M8).

Until now a workspace was created at run start and its index was thrown away when the run
ended, so nothing about the repository existed before ``POST /start`` and nothing survived
after it. A console session now owns a clone from the moment it is created, which is what
makes a warm index — and, later, grounded clarify and codebase chat — possible at all.

Not routed through the ``RunRegistry``: that queue exists because the GPU serialises model
work, and cloning plus embedding contends for neither. A failure here never fails the
session — the status fields say what happened and the session stays usable without them.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from sprint_crew.api.console.state import _lock_for, emit_for, load_session, touch
from sprint_crew.config import get_settings
from sprint_crew.orchestrator.repo_context import maybe_index_shared, should_index_workspace
from sprint_crew.orchestrator.session import prepare_workspace
from sprint_crew.schemas.console import ConsoleSession, IndexStatus, WorkspaceStatus
from sprint_crew.schemas.session import agent_event
from sprint_crew.vector.scope import index_scope_for

logger = logging.getLogger(__name__)

# Strong references to in-flight prep tasks: asyncio only holds a weak one, so a task
# nobody keeps can be garbage collected mid-clone.
_tasks: set[asyncio.Task[None]] = set()


def schedule_workspace_prep(session: ConsoleSession) -> None:
    """Start preparing this session's workspace, and say so on the session it is given.

    The caller is inside the per-session lock and will persist ``session`` on the way out,
    so the created status is written by the same save that creates the row — no second
    write, and the 201 response already says ``cloning``.
    """
    if not get_settings().console_workspace_on_create:
        session.index_status = IndexStatus.SKIPPED
        return

    session.workspace_status = WorkspaceStatus.CLONING
    task = asyncio.create_task(_prepare(session.session_id, session.repo_url))
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


async def drain_workspace_prep() -> None:
    """Await every in-flight prep task. For tests and for a clean shutdown."""
    while _tasks:
        await asyncio.gather(*tuple(_tasks), return_exceptions=True)


async def _prepare(session_id: str, repo_url: str | None) -> None:
    await emit_for(
        session_id,
        agent_event(
            "orchestrator",
            "repo_cloning",
            "Preparing repository",
            repo_url=repo_url,
        ),
    )
    try:
        workspace = await asyncio.to_thread(prepare_workspace, session_id, repo_url=repo_url)
    except Exception as exc:
        logger.exception("console workspace clone failed for %s", session_id)
        await _record(
            session_id,
            workspace_status=WorkspaceStatus.FAILED,
            workspace_error=str(exc),
            index_status=IndexStatus.SKIPPED,
        )
        await emit_for(
            session_id,
            agent_event(
                "orchestrator",
                "repo_failed",
                "Could not prepare the repository",
                level="error",
                error=str(exc)[:500],
            ),
        )
        return

    await _record(
        session_id,
        workspace_status=WorkspaceStatus.READY,
        workspace_root=str(workspace),
    )
    await emit_for(
        session_id,
        agent_event(
            "orchestrator",
            "repo_ready",
            "Repository ready",
            workspace_root=str(workspace),
            repo_url=repo_url,
        ),
    )
    await _index(session_id, workspace)


async def _index(session_id: str, workspace: Path) -> None:
    if not get_settings().console_index_on_create:
        await _record(session_id, index_status=IndexStatus.SKIPPED)
        return

    await _record(session_id, index_status=IndexStatus.INDEXING)
    loop = asyncio.get_running_loop()

    def on_progress(done: int, total: int) -> None:
        # Called from the indexing thread; hop back to the loop to publish. A first index
        # of a real repo is thousands of chunks and minutes of sidecar time — without this
        # the session sits silent through all of it.
        asyncio.run_coroutine_threadsafe(
            emit_for(
                session_id,
                agent_event(
                    "orchestrator",
                    "index_progress",
                    f"Indexing repository: {done}/{total} chunks",
                    level="debug",
                    done=done,
                    total=total,
                ),
            ),
            loop,
        )

    # A fresh clone is the repository's committed state, so this is allowed to write the
    # shared index every other session on this repo reads (vector/scope.py).
    scope = index_scope_for(workspace)
    result = await asyncio.to_thread(
        maybe_index_shared,
        workspace,
        scope,
        on_progress=on_progress,
    )
    if result is None:
        # maybe_index_shared returns None for both "not worth indexing" and "the sidecar
        # blew up", having logged the latter. Re-asking the predicate separates them —
        # reporting a dead embed sidecar as "skipped" would send the user looking in the
        # wrong place.
        applicable = await asyncio.to_thread(should_index_workspace, workspace)
        await _record(
            session_id,
            index_status=IndexStatus.FAILED if applicable else IndexStatus.SKIPPED,
            index_error="indexing failed; see server logs" if applicable else None,
        )
        return

    await _record(session_id, index_status=IndexStatus.READY)
    await emit_for(
        session_id,
        agent_event(
            "orchestrator",
            "index_ready",
            f"Repository indexed: {result.files} files",
            files=result.files,
            chunks=result.chunks,
            seconds=result.seconds,
            collection=result.collection,
        ),
    )


async def _record(session_id: str, **updates: object) -> None:
    """Write prep state under the per-session lock, re-reading first.

    Never mutate the ``ConsoleSession`` this task was handed: clarify runs concurrently on
    the same row, and a stale object saved here would silently undo its questions.
    """
    async with _lock_for(session_id):
        session = await load_session(session_id)
        if session is None:
            return  # deleted or reaped while we were preparing
        for field, value in updates.items():
            setattr(session, field, value)
        await touch(session)
