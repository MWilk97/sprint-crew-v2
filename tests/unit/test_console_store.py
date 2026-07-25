"""Durable console session store and TTL reaper (roadmap M1)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from sprint_crew.config import get_settings
from sprint_crew.orchestrator.console_store import (
    ConsoleSessionStore,
    console_store,
    reap_console_sessions,
)
from sprint_crew.schemas.console import (
    ClarifyAnswer,
    ClarifyQuestion,
    ClarifySuggestion,
    ConsoleMode,
    ConsoleSession,
    ConsoleSessionStatus,
    SprintRunRef,
)


@pytest.fixture(autouse=True)
def _tmp_store(tmp_path, monkeypatch):
    monkeypatch.setenv("SPRINT_SESSION_DB", str(tmp_path / "console.db"))
    monkeypatch.setenv("SPRINT_WORKSPACE_BASE", str(tmp_path / "workspaces"))
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _session(
    session_id: str = "cs-1",
    *,
    status: ConsoleSessionStatus = ConsoleSessionStatus.CLARIFYING,
    updated_at: str | None = None,
    sprint_session_ids: list[str] | None = None,
) -> ConsoleSession:
    session = ConsoleSession(
        session_id=session_id,
        mode=ConsoleMode.CODE,
        status=status,
        clarify_questions=[
            ClarifyQuestion(
                question_id="q-scope",
                text="Which part of the repo should change?",
                suggestions=[ClarifySuggestion(suggestion_id="s-api", label="API layer")],
                allow_custom=True,
            )
        ],
        clarify_answers=[ClarifyAnswer(question_id="q-scope", selected_suggestion_id="s-api")],
    )
    if sprint_session_ids is not None:
        session.sprint_ref = SprintRunRef(sprint_session_ids=sprint_session_ids)
    if updated_at is not None:
        session.updated_at = updated_at
    return session


def _iso_days_ago(days: float) -> str:
    return (datetime.now(tz=UTC) - timedelta(days=days)).isoformat()


def test_round_trip_survives_restart() -> None:
    session = _session(status=ConsoleSessionStatus.CLARIFYING)
    console_store().save(session)

    # A fresh store instance simulates an API restart: same file, new object.
    restored = ConsoleSessionStore(get_settings().session_db).load("cs-1")

    assert restored is not None
    assert restored.status is ConsoleSessionStatus.CLARIFYING
    assert [q.question_id for q in restored.clarify_questions] == ["q-scope"]
    assert restored.clarify_answers[0].selected_suggestion_id == "s-api"


def test_delete_and_list() -> None:
    store = console_store()
    store.save(_session("cs-1"))
    store.save(_session("cs-2"))

    assert {s.session_id for s in store.list_all()} == {"cs-1", "cs-2"}
    assert store.delete("cs-1") is True
    assert store.delete("cs-1") is False
    assert store.load("cs-1") is None
    assert {s.session_id for s in store.list_all()} == {"cs-2"}


def test_reaper_deletes_old_terminal_only() -> None:
    store = console_store()
    store.save(
        _session("cs-old", status=ConsoleSessionStatus.COMPLETED, updated_at=_iso_days_ago(30))
    )
    store.save(
        _session("cs-recent", status=ConsoleSessionStatus.COMPLETED, updated_at=_iso_days_ago(1))
    )
    store.save(
        _session("cs-running", status=ConsoleSessionStatus.RUNNING, updated_at=_iso_days_ago(30))
    )

    reaped = reap_console_sessions()

    assert reaped == ["cs-old"]
    assert store.load("cs-old") is None
    assert store.load("cs-recent") is not None
    assert store.load("cs-running") is not None


def test_reaper_deletes_workspace(tmp_path) -> None:
    workspace = get_settings().workspace_base / "sprint-abc"
    workspace.mkdir(parents=True)
    (workspace / "file.txt").write_text("x")

    console_store().save(
        _session(
            "cs-old",
            status=ConsoleSessionStatus.COMPLETED,
            updated_at=_iso_days_ago(30),
            sprint_session_ids=["sprint-abc"],
        )
    )

    reap_console_sessions()

    assert not workspace.exists()
