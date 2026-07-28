"""Codebase chat: the guard matrix, the event sequence, and the files endpoint (M9)."""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from httpx import AsyncClient

from sprint_crew.agents.explainer import ExplainerAnswer
from sprint_crew.orchestrator.run_registry import reset_run_registry, run_registry
from sprint_crew.schemas.console import AnswerCitation, ConsoleSessionStatus, WorkspaceStatus


@pytest.fixture(autouse=True)
def _clean_registry():
    reset_run_registry()
    yield
    reset_run_registry()


@contextmanager
def _explainer(answer: ExplainerAnswer | None = None, *, error: Exception | None = None):
    """Stub the model turn and the lane load; neither belongs in a unit test."""
    result = answer or ExplainerAnswer(text="It lives in src/a.py.", citations=[], tool_calls=0)
    mock = AsyncMock(side_effect=error) if error else AsyncMock(return_value=result)
    with (
        patch("sprint_crew.api.console.ask.run_explainer", mock),
        patch("sprint_crew.api.console.ask.ensure_lane", AsyncMock()),
    ):
        yield mock


async def _ready_session(api_client: AsyncClient, tmp_path: Path, **overrides) -> str:
    """A session whose checkout is ready, without cloning anything."""
    with patch("sprint_crew.api.console.routes.schedule_workspace_prep"):
        created = await api_client.post("/v1/console/sessions", json={"mode": "code"})
    session_id = created.json()["session_id"]

    workspace = tmp_path / "ws" / session_id
    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / "a.py").write_text("print('hi')\n", encoding="utf-8")

    from sprint_crew.orchestrator.console_store import console_store

    store = console_store()
    session = store.load(session_id)
    assert session is not None
    session.workspace_status = WorkspaceStatus.READY
    session.workspace_root = str(workspace)
    for key, value in overrides.items():
        setattr(session, key, value)
    store.save(session)
    return session_id


async def _drain(api_client: AsyncClient, session_id: str) -> list[dict]:
    """Let the queued ask body run to completion, then read its timeline."""
    for _ in range(200):
        session = (await api_client.get(f"/v1/console/sessions/{session_id}")).json()
        if not session["ask_in_flight"]:
            break
        await asyncio.sleep(0.01)
    events = await api_client.get(f"/v1/console/sessions/{session_id}/events")
    return events.json()["events"]


# --- the happy path -----------------------------------------------------------


async def test_ask_returns_202_with_the_question_already_recorded(
    api_client: AsyncClient, tmp_path: Path
) -> None:
    session_id = await _ready_session(api_client, tmp_path)
    with _explainer():
        response = await api_client.post(
            f"/v1/console/sessions/{session_id}/ask", json={"question": "where is a?"}
        )
        assert response.status_code == 202
        body = response.json()
        assert body["ask_in_flight"] is True
        assert body["messages"][-1]["content"] == "where is a?"
        assert body["messages"][-1]["role"] == "user"
        await _drain(api_client, session_id)


async def test_answer_streams_then_lands_as_an_assistant_message(
    api_client: AsyncClient, tmp_path: Path
) -> None:
    session_id = await _ready_session(api_client, tmp_path)
    answer = ExplainerAnswer(
        text="It lives in src/a.py.",
        citations=[AnswerCitation(path="src/a.py", start_line=3, source="read_file")],
        tool_calls=2,
    )
    with _explainer(answer) as explainer:
        await api_client.post(
            f"/v1/console/sessions/{session_id}/ask", json={"question": "where is a?"}
        )
        events = await _drain(api_client, session_id)

    kinds = [e["event_type"] for e in events]
    assert kinds.index("ask_started") < kinds.index("citation") < kinds.index("answer_complete")

    started = next(e for e in events if e["event_type"] == "ask_started")
    complete = next(e for e in events if e["event_type"] == "answer_complete")
    message_id = started["detail"]["message_id"]
    # Every event of an answer carries the id of the bubble it belongs to.
    assert complete["detail"]["message_id"] == message_id
    # answer_complete is authoritative: a client replaces the streamed bubble with this.
    assert complete["detail"]["text"] == "It lives in src/a.py."

    session = (await api_client.get(f"/v1/console/sessions/{session_id}")).json()
    last = session["messages"][-1]
    assert last["role"] == "assistant"
    assert last["message_id"] == message_id
    assert last["citations"] == [
        {"path": "src/a.py", "start_line": 3, "end_line": None, "source": "read_file"}
    ]
    assert session["ask_in_flight"] is False
    assert explainer.await_count == 1


async def test_deltas_are_emitted_as_the_answer_is_produced(
    api_client: AsyncClient, tmp_path: Path
) -> None:
    """The on_delta callback the route hands the Explainer must reach the timeline."""
    session_id = await _ready_session(api_client, tmp_path)

    async def fake_explainer(*, on_delta=None, **_kwargs):
        for chunk in ("Auth lives ", "in src/a.py."):
            on_delta(chunk)
        return ExplainerAnswer(text="Auth lives in src/a.py.", citations=[], tool_calls=0)

    with (
        patch("sprint_crew.api.console.ask.run_explainer", fake_explainer),
        patch("sprint_crew.api.console.ask.ensure_lane", AsyncMock()),
    ):
        await api_client.post(
            f"/v1/console/sessions/{session_id}/ask", json={"question": "where is auth?"}
        )
        events = await _drain(api_client, session_id)

    deltas = [e for e in events if e["event_type"] == "answer_delta"]
    assert [d["detail"]["text"] for d in deltas] == ["Auth lives ", "in src/a.py."]
    # debug level so a timeline with a level filter does not drown in them.
    assert {d["level"] for d in deltas} == {"debug"}


async def test_a_failing_explainer_ends_the_answer_instead_of_hanging(
    api_client: AsyncClient, tmp_path: Path
) -> None:
    session_id = await _ready_session(api_client, tmp_path)
    with _explainer(error=RuntimeError("lane exploded")):
        await api_client.post(f"/v1/console/sessions/{session_id}/ask", json={"question": "why?"})
        events = await _drain(api_client, session_id)

    failed = next(e for e in events if e["event_type"] == "ask_failed")
    assert "lane exploded" in failed["detail"]["error"]
    session = (await api_client.get(f"/v1/console/sessions/{session_id}")).json()
    assert session["ask_in_flight"] is False


# --- the guard matrix ---------------------------------------------------------


async def test_ask_on_unknown_session_is_404(api_client: AsyncClient) -> None:
    response = await api_client.post("/v1/console/sessions/cs-nope/ask", json={"question": "q"})
    assert response.status_code == 404


async def test_ask_before_the_checkout_is_ready_is_409(api_client: AsyncClient) -> None:
    with patch("sprint_crew.api.console.routes.schedule_workspace_prep"):
        created = await api_client.post("/v1/console/sessions", json={"mode": "code"})
    session_id = created.json()["session_id"]

    response = await api_client.post(
        f"/v1/console/sessions/{session_id}/ask", json={"question": "q"}
    )

    assert response.status_code == 409
    assert "repository is pending" in response.json()["detail"]


async def test_ask_while_this_session_runs_is_409(api_client: AsyncClient, tmp_path: Path) -> None:
    session_id = await _ready_session(api_client, tmp_path, status=ConsoleSessionStatus.RUNNING)

    response = await api_client.post(
        f"/v1/console/sessions/{session_id}/ask", json={"question": "q"}
    )

    assert response.status_code == 409
    assert "belongs to the run" in response.json()["detail"]


async def test_ask_while_another_session_runs_is_409(
    api_client: AsyncClient, tmp_path: Path
) -> None:
    """The Work lane is single-tenant, so someone else's run blocks this session too."""
    session_id = await _ready_session(api_client, tmp_path)
    started = asyncio.Event()

    async def occupy() -> None:
        started.set()
        await asyncio.sleep(60)

    run_registry().submit("run-other", occupy)
    await started.wait()

    response = await api_client.post(
        f"/v1/console/sessions/{session_id}/ask", json={"question": "q"}
    )

    assert response.status_code == 409
    assert "run-other" in response.json()["detail"]


async def test_second_ask_while_one_is_in_flight_is_409(
    api_client: AsyncClient, tmp_path: Path
) -> None:
    session_id = await _ready_session(api_client, tmp_path)
    release = asyncio.Event()

    async def slow(**_kwargs):
        await release.wait()
        return ExplainerAnswer(text="done", citations=[], tool_calls=0)

    with (
        patch("sprint_crew.api.console.ask.run_explainer", slow),
        patch("sprint_crew.api.console.ask.ensure_lane", AsyncMock()),
    ):
        await api_client.post(f"/v1/console/sessions/{session_id}/ask", json={"question": "a"})
        await asyncio.sleep(0.01)

        second = await api_client.post(
            f"/v1/console/sessions/{session_id}/ask", json={"question": "b"}
        )
        assert second.status_code == 409
        assert "already being generated" in second.json()["detail"]

        release.set()
        await _drain(api_client, session_id)


async def test_ask_is_allowed_on_a_finished_session_with_a_checkout(
    api_client: AsyncClient, tmp_path: Path
) -> None:
    """ "What did it actually change?" is asked after the run, not during it."""
    session_id = await _ready_session(api_client, tmp_path, status=ConsoleSessionStatus.COMPLETED)

    with _explainer():
        response = await api_client.post(
            f"/v1/console/sessions/{session_id}/ask", json={"question": "what changed?"}
        )
        assert response.status_code == 202
        await _drain(api_client, session_id)


async def test_ask_can_be_disabled(api_client: AsyncClient, tmp_path: Path, monkeypatch) -> None:
    from sprint_crew.config import get_settings

    session_id = await _ready_session(api_client, tmp_path)
    monkeypatch.setenv("CONSOLE_ASK_ENABLED", "false")
    get_settings.cache_clear()

    response = await api_client.post(
        f"/v1/console/sessions/{session_id}/ask", json={"question": "q"}
    )

    assert response.status_code == 409
    assert "disabled" in response.json()["detail"]


# --- ask cancel ---------------------------------------------------------------


async def test_ask_cancel_stops_the_answer_without_ending_the_session(
    api_client: AsyncClient, tmp_path: Path
) -> None:
    session_id = await _ready_session(api_client, tmp_path)

    async def never(**_kwargs):
        await asyncio.sleep(60)

    with (
        patch("sprint_crew.api.console.ask.run_explainer", never),
        patch("sprint_crew.api.console.ask.ensure_lane", AsyncMock()),
    ):
        await api_client.post(f"/v1/console/sessions/{session_id}/ask", json={"question": "a"})
        await asyncio.sleep(0.01)

        cancelled = await api_client.post(f"/v1/console/sessions/{session_id}/ask/cancel")
        assert cancelled.status_code == 200

    session = (await api_client.get(f"/v1/console/sessions/{session_id}")).json()
    # The conversation survives; only the question was abandoned.
    assert session["status"] == "collecting"


async def test_ask_cancel_without_an_ask_is_409(api_client: AsyncClient, tmp_path: Path) -> None:
    session_id = await _ready_session(api_client, tmp_path)

    response = await api_client.post(f"/v1/console/sessions/{session_id}/ask/cancel")

    assert response.status_code == 409


async def test_delete_refuses_while_an_answer_is_being_generated(
    api_client: AsyncClient, tmp_path: Path
) -> None:
    """Purging the workspace under the Explainer would leave it reading an unowned tree."""
    session_id = await _ready_session(api_client, tmp_path)

    async def never(**_kwargs):
        await asyncio.sleep(60)

    with (
        patch("sprint_crew.api.console.ask.run_explainer", never),
        patch("sprint_crew.api.console.ask.ensure_lane", AsyncMock()),
    ):
        await api_client.post(f"/v1/console/sessions/{session_id}/ask", json={"question": "a"})
        await asyncio.sleep(0.01)

        response = await api_client.delete(f"/v1/console/sessions/{session_id}")

    assert response.status_code == 409


# --- the files endpoint -------------------------------------------------------


async def test_file_endpoint_serves_a_cited_file(api_client: AsyncClient, tmp_path: Path) -> None:
    session_id = await _ready_session(api_client, tmp_path)

    response = await api_client.get(f"/v1/console/sessions/{session_id}/files/a.py")

    assert response.status_code == 200
    assert response.json()["content"] == "print('hi')\n"
    assert response.json()["truncated"] is False


@pytest.mark.parametrize(
    "escape",
    [
        "%2e%2e%2f%2e%2e%2fetc%2fpasswd",  # percent-encoded, survives client normalisation
        "%2fetc%2fpasswd",  # absolute
    ],
)
async def test_file_endpoint_refuses_to_escape_the_workspace(
    api_client: AsyncClient, tmp_path: Path, escape: str
) -> None:
    """This endpoint must not read anything an agent tool could not — same guard, same rule."""
    session_id = await _ready_session(api_client, tmp_path)

    response = await api_client.get(f"/v1/console/sessions/{session_id}/files/{escape}")

    assert response.status_code in (400, 404)
    assert "root:" not in response.text


async def test_file_endpoint_truncates_and_says_so(
    api_client: AsyncClient, tmp_path: Path, monkeypatch
) -> None:
    from sprint_crew.config import get_settings

    session_id = await _ready_session(api_client, tmp_path)
    workspace = Path(
        (await api_client.get(f"/v1/console/sessions/{session_id}")).json()["workspace_root"]
    )
    (workspace / "big.py").write_text("x" * 5000, encoding="utf-8")

    monkeypatch.setenv("CONSOLE_FILE_MAX_BYTES", "100")
    get_settings.cache_clear()

    body = (await api_client.get(f"/v1/console/sessions/{session_id}/files/big.py")).json()

    assert body["truncated"] is True
    assert len(body["content"]) == 100
    assert body["size_bytes"] == 5000


async def test_file_endpoint_404s_for_a_missing_file(
    api_client: AsyncClient, tmp_path: Path
) -> None:
    session_id = await _ready_session(api_client, tmp_path)

    response = await api_client.get(f"/v1/console/sessions/{session_id}/files/nope.py")

    assert response.status_code == 404
