from __future__ import annotations

import pytest
from tests.helpers.ac_targets import pytest_target_from_ac

# Verbatim AC prose from the 2-story from-prompt integration run that regressed.
QUEUE_AC = (
    "Queue worker processes messages by calling ferry.dispatch() and dequeues them "
    "after successful dispatch. test_queue_worker_drains_pending passes. "
    "No changes to retry_policy.py or notification routes."
)
RETRY_AC = (
    "Retry policy handles 3 attempts with delays (100ms, 200ms, 400ms). "
    "test_ferry_retry.py passes. No changes to queue worker or SQLite storage."
)


def test_queue_ac_ignores_negated_notification_mention() -> None:
    assert pytest_target_from_ac(QUEUE_AC) == "tests/test_ferry_queue.py"


def test_retry_ac_resolves_to_retry_module() -> None:
    assert pytest_target_from_ac(RETRY_AC) == "tests/test_ferry_retry.py"


@pytest.mark.parametrize(
    ("ac", "expected"),
    [
        ("pytest -q tests/test_greeter.py passes", "tests/test_greeter.py"),
        ("pytest -q tests/test_validators.py passes", "tests/test_validators.py"),
        ("pytest -q tests/test_notify_routes.py passes", "tests/test_notify_routes.py"),
    ],
)
def test_explicit_test_path_wins(ac: str, expected: str) -> None:
    assert pytest_target_from_ac(ac) == expected


def test_positive_notification_story_still_resolves() -> None:
    ac = "Notification routes return 202 for accepted payloads."
    assert pytest_target_from_ac(ac) == "tests/test_notify_routes.py"


def test_negated_only_mention_does_not_select_target() -> None:
    ac = "Greeter returns a friendly string. No changes to notification routes."
    assert pytest_target_from_ac(ac) is None


def test_ac_without_story_keywords_returns_none() -> None:
    assert pytest_target_from_ac("- Unit tests pass\n- hello() returns 'hello'") is None
