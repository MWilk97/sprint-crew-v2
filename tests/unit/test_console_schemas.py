"""Console schemas (roadmap Phase 1): validation + route registration smoke."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from sprint_crew.schemas.console import (
    ClarifyAnswer,
    ClarifyQuestion,
    ClarifyRequest,
    ClarifySuggestion,
    ConsoleMode,
    ConsoleSession,
    ConsoleSessionStatus,
    CreateConsoleSessionRequest,
    SprintRunRef,
)


def _question() -> ClarifyQuestion:
    return ClarifyQuestion(
        question_id="q-scope",
        text="Which part of the repo should change?",
        suggestions=[ClarifySuggestion(suggestion_id="s-api", label="API layer only")],
        allow_custom=True,
    )


def test_create_session_happy_path() -> None:
    req = CreateConsoleSessionRequest(mode=ConsoleMode.CODE, initial_prompt="add health endpoint")
    assert req.mode is ConsoleMode.CODE
    assert req.target_language is None


def test_session_round_trip_with_clarify_and_sprint_ref() -> None:
    session = ConsoleSession(
        session_id="cs-1",
        mode=ConsoleMode.CODE,
        status=ConsoleSessionStatus.RUNNING,
        confirmed=True,
        clarify_questions=[_question()],
        clarify_answers=[ClarifyAnswer(question_id="q-scope", selected_suggestion_id="s-api")],
        sprint_ref=SprintRunRef(backlog_run_id="run-1", sprint_session_ids=["session-a"]),
    )
    restored = ConsoleSession.model_validate(session.model_dump())
    assert restored.status is ConsoleSessionStatus.RUNNING
    assert restored.sprint_ref is not None
    assert restored.sprint_ref.backlog_run_id == "run-1"


def test_clarify_answer_requires_exactly_one() -> None:
    with pytest.raises(ValidationError):
        ClarifyAnswer(question_id="q-scope")
    with pytest.raises(ValidationError):
        ClarifyAnswer(question_id="q-scope", selected_suggestion_id="s-api", custom_text="both")
    answer = ClarifyAnswer(question_id="q-scope", custom_text="only the CLI")
    assert answer.custom_text == "only the CLI"


def test_unknown_fields_rejected() -> None:
    with pytest.raises(ValidationError):
        CreateConsoleSessionRequest(mode=ConsoleMode.PLAN, surprise=True)
    with pytest.raises(ValidationError):
        ClarifyRequest(
            answers=[ClarifyAnswer(question_id="q", custom_text="x")],
            extra_field="nope",
        )


def test_console_routes_registered() -> None:
    from sprint_crew.api.app import app

    paths = set(app.openapi()["paths"])
    expected = {
        "/v1/console/sessions",
        "/v1/console/sessions/{id}",
        "/v1/console/sessions/{id}/messages",
        "/v1/console/sessions/{id}/clarify",
        "/v1/console/sessions/{id}/confirm",
        "/v1/console/sessions/{id}/start",
        "/v1/console/sessions/{id}/cancel",
    }
    assert expected <= paths
