from __future__ import annotations

from sprint_crew.config import get_settings
from sprint_crew.inference.router import coder_model_settings, coder_thinking_active
from sprint_crew.orchestrator.acceptance_failure import analyze_acceptance_output
from sprint_crew.orchestrator.retry import format_review_feedback
from sprint_crew.schemas.change import ReviewOutcome


def _reset_settings() -> None:
    get_settings.cache_clear()


def test_coder_sampling_pinned_to_spec() -> None:
    _reset_settings()
    settings = coder_model_settings(attempt=0)
    assert settings.get("temperature") == 0.7
    assert settings.get("top_p") == 0.95
    assert settings.get("extra_body", {}).get("top_k") == 20


def test_thinking_off_before_escalation_attempt() -> None:
    _reset_settings()
    threshold = get_settings().coder_thinking_escalation_attempt
    for attempt in range(threshold):
        assert coder_thinking_active(attempt) is False
        settings = coder_model_settings(attempt=attempt)
        assert "chat_template_kwargs" not in settings.get("extra_body", {})
        assert settings.get("timeout") == get_settings().coder_request_timeout_s


def test_thinking_on_and_longer_timeout_from_escalation_attempt() -> None:
    _reset_settings()
    threshold = get_settings().coder_thinking_escalation_attempt
    assert coder_thinking_active(threshold) is True
    settings = coder_model_settings(attempt=threshold)
    assert settings["extra_body"]["chat_template_kwargs"] == {"enable_thinking": True}
    assert settings.get("timeout") == get_settings().coder_thinking_timeout_s
    assert settings.get("timeout") > get_settings().coder_request_timeout_s


def test_thinking_disabled_by_flag(monkeypatch) -> None:
    monkeypatch.setenv("CODER_THINKING_ENABLED", "false")
    _reset_settings()
    try:
        assert coder_thinking_active(99) is False
        settings = coder_model_settings(attempt=99)
        assert "chat_template_kwargs" not in settings.get("extra_body", {})
    finally:
        get_settings.cache_clear()


def test_stdlib_shadow_named_in_build_failure_feedback() -> None:
    # A collection error whose source path is a local package that shadows a
    # stdlib module (``platform``) should surface an explicit shadow hint.
    test_output = (
        "exit_code=2\n"
        "ERROR collecting tests/test_notify_routes.py\n"
        'File "src/platform/config.py", line 3, in <module>\n'
        "ImportError: cannot import name 'system' from 'platform'\n"
        "!!!!!! Interrupted: 1 error during collection !!!!!!"
    )
    analysis = analyze_acceptance_output(test_output)
    outcome = ReviewOutcome(
        ticket_key="SCRUM-3",
        passed=False,
        summary="build failure",
        tests_passed=False,
    )
    feedback = format_review_feedback(
        outcome,
        test_output=test_output,
        failure_analysis=analysis,
    )
    assert "STDLIB SHADOW DETECTED" in feedback
    assert "'platform'" in feedback


def test_no_shadow_hint_when_no_stdlib_collision() -> None:
    test_output = (
        "exit_code=2\n"
        "ERROR collecting tests/test_notify_routes.py\n"
        'File "src/messaging/queue_worker.py", line 3, in <module>\n'
        "ImportError: cannot import name 'Ferry' from 'messaging.ferry'\n"
        "!!!!!! Interrupted: 1 error during collection !!!!!!"
    )
    analysis = analyze_acceptance_output(test_output)
    outcome = ReviewOutcome(
        ticket_key="SCRUM-3",
        passed=False,
        summary="build failure",
        tests_passed=False,
    )
    feedback = format_review_feedback(
        outcome,
        test_output=test_output,
        failure_analysis=analysis,
    )
    assert "STDLIB SHADOW DETECTED" not in feedback
