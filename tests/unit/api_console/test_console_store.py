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
from sprint_crew.orchestrator.store import TypedJsonStore
from sprint_crew.schemas.console import (
    ClarifyAnswer,
    ClarifyQuestion,
    ClarifySuggestion,
    ConsoleMode,
    ConsoleSession,
    ConsoleSessionStatus,
    SprintRunRef,
)
from sprint_crew.schemas.session import BacklogRun


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


def _days_from_now(days: float) -> datetime:
    """A clock to hand the reaper.

    Age is simulated by moving the reaper's `now` forward rather than by backdating
    `updated_at`, because the store stamps that field on every save — the same rule that
    keeps the TTL honest makes a pre-backdated row unwritable.
    """
    return datetime.now(tz=UTC) + timedelta(days=days)


def test_store_factory_reuses_one_instance_per_database() -> None:
    """The factory is called per operation, and constructing a store runs `_init_db` — so a
    single `load()` opened two connections, one to re-create a table that already existed."""
    assert console_store() is console_store()


def test_store_factory_still_isolates_a_different_database(tmp_path, monkeypatch) -> None:
    """The cache is keyed on the path, so repointing SPRINT_SESSION_DB must not hand back
    the previous database — that would silently break every test's isolation."""
    first = console_store()
    monkeypatch.setenv("SPRINT_SESSION_DB", str(tmp_path / "other.db"))
    get_settings.cache_clear()

    second = console_store()

    assert second is not first
    assert second.db_path != first.db_path


def test_store_model_must_match_its_type_parameter() -> None:
    """Without this check the generic is decorative: the mismatch type-checks fine and
    only surfaces when a payload is validated in production."""
    with pytest.raises(TypeError, match="must be the same model"):

        class Mismatched(TypedJsonStore[ConsoleSession]):
            model = BacklogRun
            table = "whatever"
            key_column = "session_id"
            create_sql = "CREATE TABLE IF NOT EXISTS whatever (session_id TEXT PRIMARY KEY)"


def test_matching_store_declaration_is_accepted() -> None:
    class Matched(TypedJsonStore[ConsoleSession]):
        model = ConsoleSession
        table = "whatever"
        key_column = "session_id"
        create_sql = "CREATE TABLE IF NOT EXISTS whatever (session_id TEXT PRIMARY KEY)"

    assert Matched.model is ConsoleSession


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


def test_store_stamps_updated_at_on_every_save() -> None:
    """The reaper's TTL clock is this column, so no caller gets to forget to set it."""
    store = console_store()
    session = _session("cs-1", updated_at="2020-01-01T00:00:00+00:00")

    store.save(session)

    restored = store.load("cs-1")
    assert restored is not None
    assert restored.updated_at != "2020-01-01T00:00:00+00:00"
    # Mutated in place too: the handler returns this object in the HTTP response.
    assert session.updated_at == restored.updated_at


def test_reaper_deletes_old_terminal_only() -> None:
    store = console_store()
    store.save(_session("cs-old", status=ConsoleSessionStatus.COMPLETED))
    store.save(_session("cs-running", status=ConsoleSessionStatus.RUNNING))

    reaped = reap_console_sessions(now=_days_from_now(30))

    assert reaped == ["cs-old"]
    assert store.load("cs-old") is None
    assert store.load("cs-running") is not None


def test_reaper_keeps_sessions_inside_the_ttl() -> None:
    store = console_store()
    store.save(_session("cs-recent", status=ConsoleSessionStatus.COMPLETED))

    assert reap_console_sessions(now=_days_from_now(1)) == []
    assert store.load("cs-recent") is not None


def test_reaper_deletes_workspace(tmp_path) -> None:
    workspace = get_settings().workspace_base / "sprint-abc"
    workspace.mkdir(parents=True)
    (workspace / "file.txt").write_text("x")

    console_store().save(
        _session(
            "cs-old",
            status=ConsoleSessionStatus.COMPLETED,
            sprint_session_ids=["sprint-abc"],
        )
    )

    reap_console_sessions(now=_days_from_now(30))

    assert not workspace.exists()
