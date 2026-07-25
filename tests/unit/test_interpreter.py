"""Interpreter clarify: id assignment, recommendations, and fallback behaviour (ADR 0013)."""

from __future__ import annotations

from contextlib import contextmanager
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from sprint_crew.agents.interpreter import to_clarify_questions
from sprint_crew.config import get_settings
from sprint_crew.schemas.intent import IntentAnalysis, InterpreterOption, InterpreterQuestion

INTERPRETER_PATCH = "sprint_crew.api.console.run_interpreter"
LANE_PATCH = "sprint_crew.api.console._work_lane_available"


@pytest.fixture(autouse=True)
def clarify_llm_on(monkeypatch: pytest.MonkeyPatch):
    """Opt back into the LLM path that the global conftest fixture disables."""
    monkeypatch.setenv("CLARIFY_LLM_ENABLED", "true")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _question(**overrides) -> InterpreterQuestion:
    defaults = dict(
        text="Should /metrics require auth?",
        why_asked="The repo has no auth layer and metrics are often scraped anonymously",
        options=[
            InterpreterOption(label="No auth", rationale="matches scraper defaults"),
            InterpreterOption(label="Reuse API auth", rationale="safer, more wiring"),
        ],
        recommended_index=0,
    )
    defaults.update(overrides)
    return InterpreterQuestion(**defaults)


def _analysis(**overrides) -> IntentAnalysis:
    defaults = dict(
        restated_goal="Add a /metrics endpoint exposing request counters",
        assumptions=["Prometheus text format"],
        unknowns=["auth"],
        questions=[_question()],
        confidence=0.7,
    )
    defaults.update(overrides)
    return IntentAnalysis(**defaults)


@contextmanager
def _interpreter(*, analysis: IntentAnalysis | None = None, lane: bool = True, error=None):
    """Stub the clarify path; yields the interpreter mock for call assertions."""
    mock = AsyncMock(side_effect=error) if error is not None else AsyncMock(return_value=analysis)
    with (
        patch(LANE_PATCH, new=AsyncMock(return_value=lane)),
        patch(INTERPRETER_PATCH, new=mock),
    ):
        yield mock


def _create(client: TestClient, prompt: str = "Add a /metrics endpoint") -> dict:
    resp = client.post("/v1/console/sessions", json={"mode": "code", "initial_prompt": prompt})
    assert resp.status_code == 201
    return resp.json()


# --- conversion ---------------------------------------------------------------


def test_conversion_assigns_stable_ids_and_resolves_recommendation() -> None:
    questions = to_clarify_questions(_analysis(), limit=4)
    assert len(questions) == 1
    question = questions[0]
    assert question.question_id == "q-1"
    assert [s.suggestion_id for s in question.suggestions] == ["s-1-1", "s-1-2"]
    assert question.recommended_suggestion_id == "s-1-1"
    assert question.why_asked
    assert question.suggestions[0].rationale == "matches scraper defaults"


def test_conversion_clamps_out_of_range_recommendation() -> None:
    analysis = _analysis(questions=[_question(recommended_index=99)])
    assert to_clarify_questions(analysis, limit=4)[0].recommended_suggestion_id == "s-1-2"


def test_conversion_enforces_question_limit() -> None:
    many = [_question(text=f"q{i}") for i in range(6)]
    assert len(to_clarify_questions(_analysis(questions=many), limit=2)) == 2


# --- thinking / deadline ------------------------------------------------------


@pytest.mark.parametrize(
    ("thinking", "expected_timeout"),
    [(False, 120.0), (True, 600.0)],
)
async def test_thinking_selects_matching_deadline(thinking: bool, expected_timeout: float) -> None:
    """A reasoning trace outruns the interactive deadline, so it must swap timeouts."""
    from sprint_crew.agents import interpreter as interpreter_module

    with patch.object(
        interpreter_module, "structured_completion", return_value=_analysis()
    ) as completion:
        await interpreter_module.run_interpreter(user_prompt="anything", thinking=thinking)

    kwargs = completion.call_args.kwargs
    assert kwargs["timeout_seconds"] == expected_timeout
    assert kwargs["extra_body"] == {"chat_template_kwargs": {"enable_thinking": thinking}}


# --- API wiring ---------------------------------------------------------------


def test_create_uses_interpreter_questions(console_client: TestClient) -> None:
    with _interpreter(analysis=_analysis()):
        session = _create(console_client)
    assert session["status"] == "clarifying"
    assert len(session["clarify_questions"]) == 1
    question = session["clarify_questions"][0]
    assert question["text"] == "Should /metrics require auth?"
    assert question["recommended_suggestion_id"] == "s-1-1"
    assert session["intent"]["restated_goal"].startswith("Add a /metrics")
    assert session["intent"]["confidence"] == 0.7


def test_no_questions_goes_straight_to_ready(console_client: TestClient) -> None:
    with _interpreter(analysis=_analysis(questions=[])):
        session = _create(console_client)
    assert session["status"] == "ready"
    assert session["clarify_questions"] == []
    assert session["intent"]["restated_goal"]
    # confirm still gates the run (ADR 0012)
    assert session["confirmed"] is False


def test_interpreter_failure_falls_back_to_stub(console_client: TestClient) -> None:
    with _interpreter(error=RuntimeError("lane exploded")):
        session = _create(console_client)
    assert session["status"] == "clarifying"
    assert session["clarify_questions"][0]["question_id"] == "q-scope"
    assert session["intent"] is None


def test_cold_lane_falls_back_without_calling_model(console_client: TestClient) -> None:
    with _interpreter(lane=False) as interpreter:
        session = _create(console_client)
    assert session["status"] == "clarifying"
    assert session["clarify_questions"][0]["question_id"] == "q-scope"
    interpreter.assert_not_awaited()


def test_assumptions_reach_the_run_prompt(console_client: TestClient) -> None:
    run_mock = AsyncMock(return_value="run-1")
    with _interpreter(analysis=_analysis(questions=[])):
        session = _create(console_client)
    sid = session["session_id"]
    assert console_client.post(f"/v1/console/sessions/{sid}/confirm").status_code == 200
    with patch("sprint_crew.api.app.start_from_prompt_run", new=run_mock):
        assert console_client.post(f"/v1/console/sessions/{sid}/start").status_code == 200
    prompt = run_mock.await_args.kwargs["prompt"]
    assert "Add a /metrics endpoint" in prompt
    assert "Prometheus text format" in prompt
