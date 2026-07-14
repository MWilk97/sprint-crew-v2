from __future__ import annotations

import json
import shutil
import sqlite3
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from sprint_crew.config import get_settings
from sprint_crew.graph.pipeline import run_sprint_cycle
from sprint_crew.graph.state import SprintState
from sprint_crew.integrations.jira_client import default_git_env
from sprint_crew.schemas.change import CodeChange, ReviewOutcome, TestAdditions
from sprint_crew.schemas.session import AgentEvent, SessionStatus, SprintSession
from sprint_crew.schemas.ticket import JiraTicket, TaskPlan


def _utc_now_iso() -> str:
    return datetime.now(tz=UTC).isoformat()


class SessionStore:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS sessions (
                    session_id TEXT PRIMARY KEY,
                    payload TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )

    def save(self, session: SprintSession) -> None:
        session.updated_at = _utc_now_iso()
        payload = session.model_dump(mode="json")
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO sessions (session_id, payload, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(session_id) DO UPDATE SET
                    payload=excluded.payload,
                    updated_at=excluded.updated_at
                """,
                (session.session_id, json.dumps(payload), session.updated_at),
            )

    def load(self, session_id: str) -> SprintSession | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT payload FROM sessions WHERE session_id = ?",
                (session_id,),
            ).fetchone()
        if row is None:
            return None
        return SprintSession.model_validate(json.loads(row["payload"]))


def _store() -> SessionStore:
    return SessionStore(get_settings().session_db)


def prepare_workspace(
    session_id: str,
    source: Path | None = None,
    *,
    repo_url: str | None = None,
) -> Path:
    settings = get_settings()
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
        events=list(initial_events or []),
    )
    initial_events_snapshot = list(session.events)
    store = _store()
    store.save(session)

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
        nonlocal session
        session = _session_from_state(partial, session, initial_events=initial_events_snapshot)
        store.save(session)

    try:
        final_state = await run_sprint_cycle(state, on_node_complete=_persist_progress)
        session = _session_from_state(final_state, session, initial_events=initial_events_snapshot)
    except Exception as exc:
        import traceback

        session = session.model_copy(
            update={
                "status": SessionStatus.FAILED,
                "error": f"{exc}\n{traceback.format_exc()[-1500:]}",
                "updated_at": _utc_now_iso(),
            }
        )
    store.save(session)
    return session


def get_session(session_id: str) -> SprintSession | None:
    return _store().load(session_id)


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
    _store().save(updated)
    return updated
