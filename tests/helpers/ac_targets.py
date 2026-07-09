from __future__ import annotations

STORY_TEST_TARGETS = {
    "queue": "tests/test_ferry_queue.py",
    "retry": "tests/test_ferry_retry.py",
    "notify": "tests/test_notify_routes.py",
}


def pytest_target_from_ac(acceptance_criteria: str) -> str | None:
    """Resolve pytest module path from acceptance criteria prose."""
    for token in acceptance_criteria.split():
        if token.startswith("tests/") and token.endswith(".py"):
            return token

    lowered = acceptance_criteria.lower()
    for target in STORY_TEST_TARGETS.values():
        if target.replace("tests/", "") in lowered or target in lowered:
            return target
    if "test_ferry_queue" in lowered:
        return STORY_TEST_TARGETS["queue"]
    if "test_ferry_retry" in lowered:
        return STORY_TEST_TARGETS["retry"]
    if "test_notify" in lowered or "notification" in lowered:
        return STORY_TEST_TARGETS["notify"]
    return None
