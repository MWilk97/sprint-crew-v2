"""Multi-turn clarify and grounded clarify (roadmap M9).

The contract these pin is the one that breaks a client: a message sent while clarifying or
ready *replaces* the open question set instead of being silently ignored, so ids are
round-scoped, `ready` can go back to `clarifying`, and an answer to a retired round is a 409.
"""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from sprint_crew.config import get_settings
from sprint_crew.schemas.console import WorkspaceStatus
from sprint_crew.schemas.intent import IntentAnalysis, InterpreterOption, InterpreterQuestion


@pytest.fixture(autouse=True)
def clarify_llm_on(monkeypatch: pytest.MonkeyPatch):
    """Opt back into the LLM path that the global conftest fixture disables."""
    monkeypatch.setenv("CLARIFY_LLM_ENABLED", "true")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _analysis(*, text: str = "Which module?", n_questions: int = 1) -> IntentAnalysis:
    question = InterpreterQuestion(
        text=text,
        why_asked="two modules could own this",
        options=[
            InterpreterOption(label="src/api", rationale="thinner change"),
            InterpreterOption(label="src/core", rationale="more correct, more work"),
        ],
        recommended_index=0,
        allow_custom=True,
    )
    return IntentAnalysis(
        restated_goal="Add a metrics endpoint",
        assumptions=[],
        unknowns=[],
        questions=[question] * n_questions,
        confidence=0.7,
    )


@contextmanager
def _interpreter(analysis: IntentAnalysis | None = None):
    """Interpreter stubbed; the lane is reported healthy so clarify takes the LLM path."""
    mock = AsyncMock(return_value=analysis if analysis is not None else _analysis())
    with (
        patch("sprint_crew.api.console.clarify.run_interpreter", mock),
        patch("sprint_crew.api.console.clarify._work_lane_available", AsyncMock(return_value=True)),
        patch("sprint_crew.api.console.routes.schedule_workspace_prep"),
    ):
        yield mock


def _create(client: TestClient, prompt: str = "add metrics") -> dict:
    return client.post(
        "/v1/console/sessions", json={"mode": "code", "initial_prompt": prompt}
    ).json()


# --- rounds -------------------------------------------------------------------


def test_first_round_ids_are_round_scoped(client: TestClient) -> None:
    with _interpreter():
        session = _create(client)

    assert session["clarify_round"] == 1
    assert session["clarify_questions"][0]["question_id"] == "q-1-1"
    assert session["clarify_questions"][0]["suggestions"][0]["suggestion_id"] == "s-1-1-1"


def test_a_message_while_clarifying_replaces_the_question_set(client: TestClient) -> None:
    with _interpreter():
        session = _create(client)
        first_ids = [q["question_id"] for q in session["clarify_questions"]]

        updated = client.post(
            f"/v1/console/sessions/{session['session_id']}/messages",
            json={"content": "actually only the API layer"},
        ).json()

    assert updated["clarify_round"] == 2
    new_ids = [q["question_id"] for q in updated["clarify_questions"]]
    assert new_ids == ["q-2-1"]
    assert not set(new_ids) & set(first_ids)


def test_a_message_while_ready_goes_back_to_clarifying(client: TestClient) -> None:
    """The first backwards transition in this state machine — a client must allow it."""
    with _interpreter(_analysis(n_questions=0)):
        session = _create(client)
        assert session["status"] == "ready"

        with patch(
            "sprint_crew.api.console.clarify.run_interpreter",
            AsyncMock(return_value=_analysis()),
        ):
            updated = client.post(
                f"/v1/console/sessions/{session['session_id']}/messages",
                json={"content": "wait, also the CLI"},
            ).json()

    assert updated["status"] == "clarifying"


def test_re_interpreting_revokes_a_confirmation(client: TestClient) -> None:
    """A confirmation belongs to the understanding it was given for."""
    with _interpreter(_analysis(n_questions=0)):
        session = _create(client)
        sid = session["session_id"]
        assert client.post(f"/v1/console/sessions/{sid}/confirm").json()["confirmed"] is True

        updated = client.post(
            f"/v1/console/sessions/{sid}/messages", json={"content": "one more thing"}
        ).json()

    assert updated["confirmed"] is False


def test_answers_from_a_retired_round_move_to_prior_clarifications(client: TestClient) -> None:
    with _interpreter():
        session = _create(client)
        sid = session["session_id"]
        client.post(
            f"/v1/console/sessions/{sid}/clarify",
            json={"answers": [{"question_id": "q-1-1", "selected_suggestion_id": "s-1-1-1"}]},
        )

        updated = client.post(
            f"/v1/console/sessions/{sid}/messages", json={"content": "and the CLI too"}
        ).json()

    assert updated["clarify_answers"] == []
    assert any("src/api" in line for line in updated["prior_clarifications"])


def test_answering_a_retired_round_is_409_not_400(client: TestClient) -> None:
    """The request was correct when it was rendered; the question set moved on."""
    with _interpreter():
        session = _create(client)
        sid = session["session_id"]
        client.post(f"/v1/console/sessions/{sid}/messages", json={"content": "also the CLI"})

        response = client.post(
            f"/v1/console/sessions/{sid}/clarify",
            json={"answers": [{"question_id": "q-1-1", "selected_suggestion_id": "s-1-1-1"}]},
        )

    assert response.status_code == 409
    assert "round 1" in response.json()["detail"]


def test_a_genuinely_unknown_question_id_is_still_400(client: TestClient) -> None:
    with _interpreter():
        session = _create(client)
        response = client.post(
            f"/v1/console/sessions/{session['session_id']}/clarify",
            json={"answers": [{"question_id": "q-nonsense", "custom_text": "x"}]},
        )

    assert response.status_code == 400


def test_a_new_round_is_announced_on_the_timeline(client: TestClient) -> None:
    with _interpreter():
        session = _create(client)
        sid = session["session_id"]
        client.post(f"/v1/console/sessions/{sid}/messages", json={"content": "also the CLI"})

    events = client.get(f"/v1/console/sessions/{sid}/events").json()["events"]
    rounds = [e for e in events if e["event_type"] == "clarify_round_started"]
    assert [e["detail"]["round"] for e in rounds] == [2]


def test_the_first_clarify_is_not_a_new_round(client: TestClient) -> None:
    """Creating a session must not look like a revision of something."""
    with _interpreter():
        session = _create(client)

    events = client.get(f"/v1/console/sessions/{session['session_id']}/events").json()["events"]
    assert not [e for e in events if e["event_type"] == "clarify_round_started"]


def test_every_round_reaches_the_run_prompt(client: TestClient) -> None:
    """An answer given in round 1 is still a decision the run has to honour."""
    from sprint_crew.api.console.run_bridge import build_run_prompt
    from sprint_crew.orchestrator.console_store import console_store

    with _interpreter():
        session = _create(client)
        sid = session["session_id"]
        client.post(
            f"/v1/console/sessions/{sid}/clarify",
            json={"answers": [{"question_id": "q-1-1", "selected_suggestion_id": "s-1-1-1"}]},
        )
        client.post(f"/v1/console/sessions/{sid}/messages", json={"content": "and the CLI"})
        client.post(
            f"/v1/console/sessions/{sid}/clarify",
            json={"answers": [{"question_id": "q-2-1", "selected_suggestion_id": "s-2-1-2"}]},
        )

    prompt = build_run_prompt(console_store().load(sid))

    assert "src/api" in prompt  # round 1
    assert "src/core" in prompt  # round 2


# --- grounding ----------------------------------------------------------------


def test_clarify_passes_repo_context_once_the_checkout_is_ready(
    client: TestClient, tmp_path: Path
) -> None:
    with _interpreter() as interpreter:
        session = _create(client)
        sid = session["session_id"]

        from sprint_crew.orchestrator.console_store import console_store

        store = console_store()
        stored = store.load(sid)
        workspace = tmp_path / "ws"
        workspace.mkdir()
        stored.workspace_status = WorkspaceStatus.READY
        stored.workspace_root = str(workspace)
        store.save(stored)

        with patch(
            "sprint_crew.api.console.clarify._gather_repo_context",
            return_value="=== repo_manifest ===\nsrc/api/metrics.py",
        ):
            client.post(f"/v1/console/sessions/{sid}/messages", json={"content": "more"})

    assert "src/api/metrics.py" in interpreter.await_args.kwargs["repo_context"]


def test_clarify_without_a_checkout_stays_ungrounded(client: TestClient) -> None:
    """M8 publishes readiness; a session still cloning must not block on it (ADR 0013)."""
    with _interpreter() as interpreter:
        _create(client)

    assert interpreter.await_args.kwargs["repo_context"] == ""


def test_a_failing_repo_gather_degrades_instead_of_failing_the_request(
    client: TestClient, tmp_path: Path
) -> None:
    with _interpreter() as interpreter:
        session = _create(client)
        sid = session["session_id"]

        from sprint_crew.orchestrator.console_store import console_store

        store = console_store()
        stored = store.load(sid)
        workspace = tmp_path / "ws"
        workspace.mkdir()
        stored.workspace_status = WorkspaceStatus.READY
        stored.workspace_root = str(workspace)
        store.save(stored)

        with patch(
            "sprint_crew.api.console.clarify._gather_repo_context",
            side_effect=RuntimeError("qdrant is down"),
        ):
            response = client.post(f"/v1/console/sessions/{sid}/messages", json={"content": "more"})

    assert response.status_code == 200
    assert interpreter.await_args.kwargs["repo_context"] == ""


def test_repo_context_is_capped(client: TestClient, tmp_path: Path, monkeypatch) -> None:
    """Unbounded, manifest + grep + pre-search would blow the interactive deadline."""
    from sprint_crew.config import get_settings

    monkeypatch.setenv("INTERPRETER_REPO_CONTEXT_CHARS", "200")
    get_settings.cache_clear()

    with _interpreter() as interpreter:
        session = _create(client)
        sid = session["session_id"]

        from sprint_crew.orchestrator.console_store import console_store

        store = console_store()
        stored = store.load(sid)
        workspace = tmp_path / "ws"
        workspace.mkdir()
        stored.workspace_status = WorkspaceStatus.READY
        stored.workspace_root = str(workspace)
        store.save(stored)

        with patch(
            "sprint_crew.api.console.clarify._gather_repo_context", return_value="x" * 10_000
        ):
            client.post(f"/v1/console/sessions/{sid}/messages", json={"content": "more"})

    context = interpreter.await_args.kwargs["repo_context"]
    assert len(context) < 300
    assert context.endswith("(repository context truncated)")


def test_prior_clarifications_reach_the_interpreter_on_a_later_round(
    client: TestClient,
) -> None:
    """The point of re-interpreting is to not ask the same thing twice."""
    with _interpreter() as interpreter:
        session = _create(client)
        sid = session["session_id"]
        client.post(
            f"/v1/console/sessions/{sid}/clarify",
            json={"answers": [{"question_id": "q-1-1", "selected_suggestion_id": "s-1-1-1"}]},
        )
        client.post(f"/v1/console/sessions/{sid}/messages", json={"content": "and the CLI"})

    conversation = interpreter.await_args.kwargs["conversation"]
    assert "src/api" in conversation


@pytest.mark.parametrize("enabled", ["true", "false"])
def test_grounding_can_be_switched_off(
    client: TestClient, tmp_path: Path, monkeypatch, enabled: str
) -> None:
    from sprint_crew.config import get_settings

    monkeypatch.setenv("CLARIFY_REPO_CONTEXT_ENABLED", enabled)
    get_settings.cache_clear()

    with _interpreter() as interpreter:
        session = _create(client)
        sid = session["session_id"]

        from sprint_crew.orchestrator.console_store import console_store

        store = console_store()
        stored = store.load(sid)
        workspace = tmp_path / "ws"
        workspace.mkdir()
        stored.workspace_status = WorkspaceStatus.READY
        stored.workspace_root = str(workspace)
        store.save(stored)

        with patch("sprint_crew.api.console.clarify._gather_repo_context", return_value="REPO"):
            client.post(f"/v1/console/sessions/{sid}/messages", json={"content": "more"})

    assert (interpreter.await_args.kwargs["repo_context"] == "REPO") is (enabled == "true")
