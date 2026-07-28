"""/v1/console/* routes: state machine, error codes, plan + code start paths."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from sprint_crew.api import console as console_module
from sprint_crew.api.console.clarify import apply_clarify_answers
from sprint_crew.config import get_settings
from sprint_crew.orchestrator.backlog import BacklogRunStore
from sprint_crew.schemas.console import (
    ClarifyAnswer,
    ClarifyQuestion,
    ClarifySuggestion,
    ConsoleMode,
    ConsoleSession,
    ConsoleSessionStatus,
)
from sprint_crew.schemas.session import BacklogRun, BacklogRunStatus

START_RUN_PATCH = "sprint_crew.api.app.start_from_prompt_run"
PLAN_BODY_PATCH = "sprint_crew.api.console.plan._plan_body"


def _create(client: TestClient, mode: str = "plan", prompt: str | None = "Add a hello() helper"):
    resp = client.post(
        "/v1/console/sessions",
        json={"mode": mode, "initial_prompt": prompt},
    )
    assert resp.status_code == 201
    return resp.json()


def _answer_all(client: TestClient, session: dict) -> dict:
    answers = [
        {
            "question_id": q["question_id"],
            "selected_suggestion_id": q["suggestions"][0]["suggestion_id"],
            "custom_text": None,
        }
        for q in session["clarify_questions"]
    ]
    resp = client.post(
        f"/v1/console/sessions/{session['session_id']}/clarify", json={"answers": answers}
    )
    assert resp.status_code == 200
    return resp.json()


def _make_ready_confirmed(client: TestClient, mode: str = "plan") -> dict:
    session = _create(client, mode=mode)
    session = _answer_all(client, session)
    assert session["status"] == "ready"
    resp = client.post(f"/v1/console/sessions/{session['session_id']}/confirm")
    assert resp.status_code == 200
    return resp.json()


# --- create / get / messages -------------------------------------------------


def test_create_with_prompt_enters_clarifying(client: TestClient) -> None:
    session = _create(client, mode="code", prompt="Add a /metrics endpoint")
    assert session["status"] == "clarifying"
    assert session["confirmed"] is False
    assert session["sprint_ref"] is None
    assert session["plan_result"] is None
    assert session["messages"][0]["role"] == "user"
    assert 2 <= len(session["clarify_questions"]) <= 3
    for question in session["clarify_questions"]:
        assert question["suggestions"]
        assert question["allow_custom"] is True


def test_session_survives_restart(client: TestClient) -> None:
    # No in-memory dict: every handler loads from the store, so dropping the
    # process-local locks (a restart) must not lose the session.
    session = _create(client, mode="code", prompt="Add a /metrics endpoint")
    sid = session["session_id"]
    console_module.reset_console_locks()

    resp = client.get(f"/v1/console/sessions/{sid}")
    assert resp.status_code == 200
    assert resp.json()["status"] == "clarifying"


@pytest.mark.parametrize(
    "path",
    [
        "/v1/console/sessions/cs-nope",
        "/v1/console/sessions/cs-nope/events",
    ],
)
def test_unknown_session_does_not_allocate_a_lock(client: TestClient, path: str) -> None:
    """Every handler used to lock first and look up second, so any made-up id minted a
    permanent lock entry. The reaper only evicts ids that were real sessions, so nothing
    reclaimed them — an unbounded dict fed by 404s.
    """
    console_module.reset_console_locks()

    for index in range(25):
        assert client.get(f"{path}-{index}").status_code == 404

    assert console_module.lock_count() == 0


def test_unknown_session_post_routes_do_not_allocate_a_lock(client: TestClient) -> None:
    console_module.reset_console_locks()

    for suffix, body in (
        ("messages", {"content": "hi"}),
        ("clarify", {"answers": [{"question_id": "q-scope", "selected_suggestion_id": "s-x"}]}),
        ("confirm", None),
        ("start", None),
        ("cancel", None),
    ):
        resp = client.post(f"/v1/console/sessions/cs-nope/{suffix}", json=body)
        assert resp.status_code == 404, suffix

    assert console_module.lock_count() == 0


def test_real_session_still_gets_one_lock(client: TestClient) -> None:
    """The guard must not have been implemented by never locking at all."""
    console_module.reset_console_locks()
    session = _create(client, mode="plan")

    client.get(f"/v1/console/sessions/{session['session_id']}")

    assert console_module.lock_count() == 1


def test_get_after_reap_returns_404(client: TestClient, monkeypatch) -> None:
    monkeypatch.setenv("CONSOLE_SESSION_TTL_DAYS", "0")
    get_settings.cache_clear()
    session = _make_ready_confirmed(client, mode="plan")
    sid = session["session_id"]

    # Cancel rather than start: since M10 no route reaches a terminal status inside the
    # request, and this test is about the reaper, not about how the session ended.
    resp = client.post(f"/v1/console/sessions/{sid}/cancel")
    assert resp.status_code == 200
    assert resp.json()["status"] == "cancelled"

    # TTL=0: the completion sweep reaps the just-finished session immediately.
    assert client.get(f"/v1/console/sessions/{sid}").status_code == 404


def test_create_without_prompt_collecting_then_message_clarifies(client: TestClient) -> None:
    session = _create(client, prompt=None)
    assert session["status"] == "collecting"
    assert session["clarify_questions"] == []

    resp = client.post(
        f"/v1/console/sessions/{session['session_id']}/messages",
        json={"content": "Add a hello() helper"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "clarifying"
    assert body["clarify_questions"]


def test_get_returns_session_and_404_for_unknown(client: TestClient) -> None:
    session = _create(client)
    resp = client.get(f"/v1/console/sessions/{session['session_id']}")
    assert resp.status_code == 200
    assert resp.json()["session_id"] == session["session_id"]

    assert client.get("/v1/console/sessions/missing").status_code == 404
    valid_bodies = {
        "messages": {"content": "x"},
        "clarify": {"answers": [{"question_id": "q", "custom_text": "x"}]},
        "confirm": None,
        "start": None,
        "cancel": None,
    }
    for action, body in valid_bodies.items():
        resp = client.post(f"/v1/console/sessions/missing/{action}", json=body)
        assert resp.status_code == 404, action


def test_create_validation_422(client: TestClient) -> None:
    resp = client.post("/v1/console/sessions", json={"mode": "plan", "surprise": True})
    assert resp.status_code == 422
    resp = client.post("/v1/console/sessions", json={"mode": "yolo"})
    assert resp.status_code == 422


def test_message_rejected_when_terminal_or_running(client: TestClient) -> None:
    session = _create(client)
    sid = session["session_id"]
    assert client.post(f"/v1/console/sessions/{sid}/cancel").status_code == 200
    resp = client.post(f"/v1/console/sessions/{sid}/messages", json={"content": "more"})
    assert resp.status_code == 409

    with patch(START_RUN_PATCH, new=AsyncMock(return_value="run-1")):
        running = _make_ready_confirmed(client, mode="code")
        rid = running["session_id"]
        assert client.post(f"/v1/console/sessions/{rid}/start").status_code == 202
    resp = client.post(f"/v1/console/sessions/{rid}/messages", json={"content": "more"})
    assert resp.status_code == 409


def test_message_empty_content_422(client: TestClient) -> None:
    session = _create(client)
    resp = client.post(
        f"/v1/console/sessions/{session['session_id']}/messages", json={"content": ""}
    )
    assert resp.status_code == 422


# --- clarify ------------------------------------------------------------------


def test_clarify_partial_then_ready(client: TestClient) -> None:
    session = _create(client, mode="code")
    sid = session["session_id"]
    questions = session["clarify_questions"]

    first = questions[0]
    resp = client.post(
        f"/v1/console/sessions/{sid}/clarify",
        json={
            "answers": [
                {
                    "question_id": first["question_id"],
                    "selected_suggestion_id": first["suggestions"][0]["suggestion_id"],
                    "custom_text": None,
                }
            ]
        },
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "clarifying"

    rest = [
        {"question_id": q["question_id"], "selected_suggestion_id": None, "custom_text": "custom"}
        for q in questions[1:]
    ]
    resp = client.post(f"/v1/console/sessions/{sid}/clarify", json={"answers": rest})
    assert resp.status_code == 200
    assert resp.json()["status"] == "ready"


def test_clarify_errors(client: TestClient) -> None:
    session = _create(client)
    sid = session["session_id"]
    question = session["clarify_questions"][0]

    resp = client.post(
        f"/v1/console/sessions/{sid}/clarify",
        json={"answers": [{"question_id": "q-nope", "custom_text": "x"}]},
    )
    assert resp.status_code == 400

    resp = client.post(
        f"/v1/console/sessions/{sid}/clarify",
        json={
            "answers": [
                {"question_id": question["question_id"], "selected_suggestion_id": "s-nope"}
            ]
        },
    )
    assert resp.status_code == 400

    # both / neither answer fields set → schema-level 422
    resp = client.post(
        f"/v1/console/sessions/{sid}/clarify",
        json={"answers": [{"question_id": question["question_id"]}]},
    )
    assert resp.status_code == 422

    # duplicate answer to an already-answered question
    good = {
        "question_id": question["question_id"],
        "selected_suggestion_id": question["suggestions"][0]["suggestion_id"],
    }
    assert (
        client.post(f"/v1/console/sessions/{sid}/clarify", json={"answers": [good]}).status_code
        == 200
    )
    resp = client.post(f"/v1/console/sessions/{sid}/clarify", json={"answers": [good]})
    assert resp.status_code == 400


def test_clarify_wrong_state_409(client: TestClient) -> None:
    session = _create(client, prompt=None)  # collecting
    resp = client.post(
        f"/v1/console/sessions/{session['session_id']}/clarify",
        json={"answers": [{"question_id": "q-scope", "custom_text": "x"}]},
    )
    assert resp.status_code == 409


def test_apply_clarify_answers_rejects_custom_when_not_allowed() -> None:
    session = ConsoleSession(
        session_id="cs-unit",
        mode=ConsoleMode.PLAN,
        status=ConsoleSessionStatus.CLARIFYING,
        clarify_questions=[
            ClarifyQuestion(
                question_id="q-strict",
                text="Pick one",
                suggestions=[ClarifySuggestion(suggestion_id="s-1", label="one")],
                allow_custom=False,
            )
        ],
    )
    with pytest.raises(ValueError, match="does not allow a custom answer"):
        apply_clarify_answers(session, [ClarifyAnswer(question_id="q-strict", custom_text="two")])
    apply_clarify_answers(
        session, [ClarifyAnswer(question_id="q-strict", selected_suggestion_id="s-1")]
    )
    assert session.status is ConsoleSessionStatus.READY


# --- confirm / start ----------------------------------------------------------


def test_confirm_requires_ready(client: TestClient) -> None:
    session = _create(client)
    resp = client.post(f"/v1/console/sessions/{session['session_id']}/confirm")
    assert resp.status_code == 409

    ready = _answer_all(client, session)
    resp = client.post(f"/v1/console/sessions/{ready['session_id']}/confirm")
    assert resp.status_code == 200
    body = resp.json()
    assert body["confirmed"] is True
    assert body["status"] == "ready"


def test_start_guards(client: TestClient) -> None:
    session = _create(client)
    sid = session["session_id"]
    # clarifying → 409
    assert client.post(f"/v1/console/sessions/{sid}/start").status_code == 409

    ready = _answer_all(client, session)
    assert ready["status"] == "ready"
    # ready but unconfirmed → 409 with contract detail
    resp = client.post(f"/v1/console/sessions/{sid}/start")
    assert resp.status_code == 409
    assert resp.json()["detail"] == "session must be confirmed before start"


def test_plan_start_queues_a_run_instead_of_answering_inline(client: TestClient) -> None:
    """M10: plan mode is a real ScrumMaster run, so start accepts rather than completes.

    The result arrives on the event stream; see test_console_plan.py for the run itself.
    """
    session = _make_ready_confirmed(client, mode="plan")
    sid = session["session_id"]
    with patch(PLAN_BODY_PATCH, new=AsyncMock()):
        resp = client.post(f"/v1/console/sessions/{sid}/start")
        assert resp.status_code == 202
        body = resp.json()
        assert body["status"] in ("queued", "running")
        assert body["plan_result"] is None
        assert body["plan_run_id"]
        assert body["sprint_ref"] is None
        # started, so a second start is rejected exactly as it is for code mode
        assert client.post(f"/v1/console/sessions/{sid}/start").status_code == 409


def test_code_start_sets_sprint_ref_with_mocked_orchestration(client: TestClient) -> None:
    run_mock = AsyncMock(return_value="run-91c2")
    with patch(START_RUN_PATCH, new=run_mock):
        session = _make_ready_confirmed(client, mode="code")
        resp = client.post(f"/v1/console/sessions/{session['session_id']}/start")
    assert resp.status_code == 202
    body = resp.json()
    assert body["status"] == "running"
    assert body["sprint_ref"]["backlog_run_id"] == "run-91c2"
    assert body["sprint_ref"]["sprint_session_ids"] == []
    run_mock.assert_awaited_once()
    prompt = run_mock.await_args.kwargs["prompt"]
    assert "Add a hello() helper" in prompt
    assert "Clarifications:" in prompt


def test_get_running_code_session_mirrors_backlog(client: TestClient, api_db) -> None:
    with patch(START_RUN_PATCH, new=AsyncMock(return_value="run-mirror")):
        session = _make_ready_confirmed(client, mode="code")
        sid = session["session_id"]
        assert client.post(f"/v1/console/sessions/{sid}/start").status_code == 202

    store = BacklogRunStore(api_db)
    store.save(
        BacklogRun(
            run_id="run-mirror",
            status=BacklogRunStatus.RUNNING,
            user_prompt="x",
            session_ids=["s1", "s2"],
        )
    )
    body = client.get(f"/v1/console/sessions/{sid}").json()
    assert body["status"] == "running"
    assert body["sprint_ref"]["sprint_session_ids"] == ["s1", "s2"]

    store.save(
        BacklogRun(
            run_id="run-mirror",
            status=BacklogRunStatus.COMPLETED,
            user_prompt="x",
            session_ids=["s1", "s2"],
        )
    )
    body = client.get(f"/v1/console/sessions/{sid}").json()
    assert body["status"] == "completed"


def test_code_start_failure_marks_session_failed(client: TestClient) -> None:
    with patch(START_RUN_PATCH, new=AsyncMock(side_effect=RuntimeError("lane down"))):
        session = _make_ready_confirmed(client, mode="code")
        sid = session["session_id"]
        with pytest.raises(RuntimeError, match="lane down"):
            client.post(f"/v1/console/sessions/{sid}/start")
    body = client.get(f"/v1/console/sessions/{sid}").json()
    assert body["status"] == "failed"
    assert body["error"] == "lane down"


# --- cancel -------------------------------------------------------------------


def test_cancel_non_terminal_and_terminal(client: TestClient) -> None:
    session = _create(client, prompt=None)
    sid = session["session_id"]
    resp = client.post(f"/v1/console/sessions/{sid}/cancel")
    assert resp.status_code == 200
    assert resp.json()["status"] == "cancelled"
    assert client.post(f"/v1/console/sessions/{sid}/cancel").status_code == 409


def test_cancel_running_code_session(client: TestClient) -> None:
    with patch(START_RUN_PATCH, new=AsyncMock(return_value="run-c")):
        session = _make_ready_confirmed(client, mode="code")
        sid = session["session_id"]
        assert client.post(f"/v1/console/sessions/{sid}/start").status_code == 202
    resp = client.post(f"/v1/console/sessions/{sid}/cancel")
    assert resp.status_code == 200
    assert resp.json()["status"] == "cancelled"


# --- existing API untouched ---------------------------------------------------


def test_health_still_live(client: TestClient) -> None:
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"
