from __future__ import annotations

import asyncio
import shutil
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from sprint_crew.config import get_settings
from sprint_crew.git_exec import default_git_env
from sprint_crew.graph.pipeline import run_sprint_cycle
from sprint_crew.graph.state import SprintState
from sprint_crew.orchestrator.emitter import Emitter, emit_live, reset_emitter, set_emitter
from sprint_crew.orchestrator.event_log import event_log
from sprint_crew.orchestrator.run_registry import RunCancelled
from sprint_crew.orchestrator.store import TypedJsonStore
from sprint_crew.schemas.change import CodeChange, ReviewOutcome, TestAdditions
from sprint_crew.schemas.session import AgentEvent, SessionStatus, SprintSession, agent_event
from sprint_crew.schemas.ticket import JiraTicket, TaskPlan


def _utc_now_iso() -> str:
    return datetime.now(tz=UTC).isoformat()


class SessionStore(TypedJsonStore[SprintSession]):
    model = SprintSession
    tracks_updated_at = True
    table = "sessions"
    key_column = "session_id"
    create_sql = """
        CREATE TABLE IF NOT EXISTS sessions (
            session_id TEXT PRIMARY KEY,
            payload TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
    """

    def save(self, item: SprintSession) -> None:
        # Stamped here rather than by callers: every write is a state change worth dating,
        # and the workspace reaper reads this column.
        item.updated_at = _utc_now_iso()
        super().save(item)


def session_store() -> SessionStore:
    return SessionStore(get_settings().session_db)


def collect_stale_workspaces(*, keep: str | None = None) -> list[str]:
    """Stopgap GC: delete workspace clones older than ``WORKSPACE_TTL_DAYS``.

    Age-based on directory mtime so an in-flight workspace (recently written) is never
    removed; ``keep`` protects the session about to be (re)created. This exists only
    because workspaces are otherwise never collected.

    TODO(M8): delete this and its call site once the store-aware reaper lands.
    """
    settings = get_settings()
    base = settings.workspace_base
    if not base.exists():
        return []
    cutoff = time.time() - settings.workspace_ttl_days * 86400.0
    removed: list[str] = []
    for child in base.iterdir():
        if not child.is_dir() or (keep is not None and child.name == keep):
            continue
        try:
            if child.stat().st_mtime >= cutoff:
                continue
            shutil.rmtree(child)
        except OSError:
            continue
        removed.append(child.name)
    return removed


def prepare_workspace(
    session_id: str,
    source: Path | None = None,
    *,
    repo_url: str | None = None,
) -> Path:
    settings = get_settings()
    collect_stale_workspaces(keep=session_id)
    dest = settings.workspace_base / session_id
    if dest.exists():
        shutil.rmtree(dest)

    if repo_url:
        dest.mkdir(parents=True, exist_ok=True)
        proc = subprocess.run(
            ["git", "clone", "--depth", "1", repo_url, str(dest)],
            capture_output=True,
            text=True,
            check=False,
        )
        if proc.returncode != 0:
            raise RuntimeError(f"git clone failed: {proc.stderr or proc.stdout}")
        return dest.resolve()

    dest.mkdir(parents=True, exist_ok=True)

    fixture = source or settings.project_root / "fixtures" / "repo"
    shutil.copytree(fixture, dest, dirs_exist_ok=True)
    env = default_git_env()
    subprocess.run(["git", "init"], cwd=dest, check=True, capture_output=True)
    subprocess.run(["git", "add", "-A"], cwd=dest, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "init workspace"],
        cwd=dest,
        check=True,
        capture_output=True,
        env=env,
    )
    return dest.resolve()


def prepare_chained_workspace(parent_workspace: Path, session_id: str) -> Path:
    """Copy a shipped workspace into a new session directory on a fresh branch."""
    settings = get_settings()
    dest = settings.workspace_base / session_id
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(parent_workspace, dest, dirs_exist_ok=True)
    env = default_git_env()
    branch = f"feature/{session_id[:8]}"
    subprocess.run(
        ["git", "checkout", "-b", branch],
        cwd=dest,
        check=True,
        capture_output=True,
        env=env,
    )
    return dest.resolve()


def _state_from_session(
    session: SprintSession,
    *,
    use_real_ship: bool = False,
    deadline_epoch: float = 0.0,
) -> SprintState:
    ticket = session.selected_ticket
    if ticket is None:
        raise ValueError("selected_ticket is required")
    return {
        "session_id": session.session_id,
        "workspace_root": session.workspace_root,
        "selected_ticket": ticket.model_dump(),
        "attempt": session.attempt,
        "status": SessionStatus.RUNNING,
        "events": [],
        "error": None,
        "use_real_ship": use_real_ship,
        "deadline_epoch": deadline_epoch,
        "backlog_run_id": session.backlog_run_id,
    }


def _session_from_state(
    state: dict[str, Any],
    base: SprintSession,
    *,
    initial_events: list[AgentEvent],
) -> SprintSession:
    updates: dict[str, Any] = {
        "status": state.get("status", base.status),
        "attempt": state.get("attempt", base.attempt),
        "error": state.get("error"),
        "branch": state.get("branch", base.branch),
        "pr_url": state.get("pr_url", base.pr_url),
        "updated_at": _utc_now_iso(),
    }
    if task_plan := state.get("task_plan"):
        updates["task_plan"] = TaskPlan.model_validate(task_plan)
        updates["ticket_key"] = updates["task_plan"].ticket_key
    if code_change := state.get("code_change"):
        updates["code_change"] = CodeChange.model_validate(code_change)
    if review_outcome := state.get("review_outcome"):
        updates["review_outcome"] = ReviewOutcome.model_validate(review_outcome)
    if test_additions := state.get("test_additions"):
        updates["test_additions"] = TestAdditions.model_validate(test_additions)

    new_events = state.get("events") or []
    updates["events"] = list(initial_events) + list(new_events)
    return base.model_copy(update=updates)


async def create_and_run_cycle(
    *,
    ticket: JiraTicket,
    workspace: Path | None = None,
    session_id: str | None = None,
    user_prompt: str | None = None,
    use_real_ship: bool = False,
    max_wall_seconds: float | None = None,
    backlog_run_id: str | None = None,
    console_session_id: str | None = None,
    initial_events: list[AgentEvent] | None = None,
) -> SprintSession:
    sid = session_id or str(uuid4())
    workspace_root = workspace or prepare_workspace(sid)
    session = SprintSession(
        session_id=sid,
        status=SessionStatus.RUNNING,
        ticket_key=ticket.key,
        workspace_root=str(workspace_root),
        selected_ticket=ticket,
        user_prompt=user_prompt,
        backlog_run_id=backlog_run_id,
        console_session_id=console_session_id,
        events=list(initial_events or []),
    )
    initial_events_snapshot = list(session.events)
    store = session_store()
    store.save(session)
    # M2 timeline: mirror events into the console-scoped append-only log. ``seq`` is
    # monotonic per console session across every sprint session it spawns. Only the
    # per-node delta is appended each tick — on_node_complete hands us the full
    # accumulated list, so we slice past what we already logged.
    log = event_log() if console_session_id else None
    if log is not None and initial_events_snapshot:
        log.append_many(console_session_id, sid, initial_events_snapshot)
    logged_event_count = len(initial_events_snapshot)
    # M4: a live emitter lets tool/lane/phase events reach the table and SSE bus mid-node.
    # When set, it is the sole writer of ``tool_call`` events, so ``_persist_progress`` drops
    # them from the node-end delta below to avoid a double append.
    emitter = (
        Emitter(log=log, console_session_id=console_session_id, sprint_session_id=sid)
        if log is not None
        else None
    )

    deadline_epoch = (
        time.time() + max_wall_seconds
        if max_wall_seconds is not None and max_wall_seconds > 0
        else 0.0
    )
    state = _state_from_session(
        session,
        use_real_ship=use_real_ship,
        deadline_epoch=deadline_epoch,
    )

    async def _persist_progress(partial: dict[str, Any]) -> None:
        nonlocal session, logged_event_count
        session = _session_from_state(partial, session, initial_events=initial_events_snapshot)
        store.save(session)
        if log is not None:
            new_events = session.events[logged_event_count:]
            logged_event_count += len(new_events)
            # An event that already carries a seq was published live by the emitter and is
            # already in the timeline table; keep it in ``session.events`` (store durability
            # / legacy /sprint endpoint) but do not append it twice. Identity, not event
            # type — a new kind of live event needs no change here.
            new_events = [e for e in new_events if e.seq is None]
            if new_events:
                log.append_many(console_session_id, sid, new_events)

    token = set_emitter(emitter)
    try:
        final_state = await run_sprint_cycle(state, on_node_complete=_persist_progress)
        session = _session_from_state(final_state, session, initial_events=initial_events_snapshot)
    except (RunCancelled, asyncio.CancelledError) as exc:
        # Both cancel paths must land on CANCELLED, not FAILED. RunCancelled would
        # otherwise be swallowed by the arm below; CancelledError is a BaseException and
        # would skip it entirely, leaving the row stuck at RUNNING. The cancelled event is
        # published here rather than by a graph node — the graph is unwinding, not routing.
        cancelled = agent_event(
            "orchestrator",
            "cancelled",
            "Run cancelled",
            level="warning",
            reason=str(exc) or None,
            hard=isinstance(exc, asyncio.CancelledError),
        )
        session = session.model_copy(
            update={
                "status": SessionStatus.CANCELLED,
                "error": str(exc) or "cancelled",
                "updated_at": _utc_now_iso(),
                "events": [*session.events, emit_live(cancelled) or cancelled],
            }
        )
        store.save(session)
        if isinstance(exc, asyncio.CancelledError):
            raise
        return session
    except Exception as exc:
        import traceback

        session = session.model_copy(
            update={
                "status": SessionStatus.FAILED,
                "error": f"{exc}\n{traceback.format_exc()[-1500:]}",
                "updated_at": _utc_now_iso(),
            }
        )
    finally:
        reset_emitter(token)
    store.save(session)
    return session


def get_session(session_id: str) -> SprintSession | None:
    return session_store().load(session_id)


def approve_session(session_id: str) -> SprintSession:
    session = get_session(session_id)
    if session is None:
        raise KeyError(f"Session not found: {session_id}")
    if session.status != SessionStatus.AWAITING_HUMAN:
        raise ValueError(f"Session {session_id} is not awaiting human approval")
    updated = session.model_copy(
        update={
            "status": SessionStatus.APPROVED,
            "updated_at": _utc_now_iso(),
            "events": [
                *session.events,
                AgentEvent(
                    agent="orchestrator",
                    event_type="approved",
                    summary="Human approved (no auto-merge)",
                ),
            ],
        }
    )
    session_store().save(updated)
    return updated
