from __future__ import annotations

import re

STORY_TEST_TARGETS = {
    "queue": "tests/test_ferry_queue.py",
    "retry": "tests/test_ferry_retry.py",
    "notify": "tests/test_notify_routes.py",
}

_STORY_KEYWORDS = (
    ("queue", STORY_TEST_TARGETS["queue"]),
    ("retry", STORY_TEST_TARGETS["retry"]),
    ("notification", STORY_TEST_TARGETS["notify"]),
    ("notify", STORY_TEST_TARGETS["notify"]),
)

# Punctuation + space + capital, so "retry_policy.py or ..." stays one clause.
_SENTENCE_SPLIT = re.compile(r"[.;]\s+(?=[A-Z])")

_NEGATION_PREFIXES = (
    "no ",
    "no-",
    "not ",
    "never",
    "without",
    "don't",
    "do not",
    "must not",
    "should not",
)


def _positive_clauses(acceptance_criteria: str) -> str:
    """Drop clauses that scope work negatively ("No changes to X")."""
    parts = _SENTENCE_SPLIT.split(acceptance_criteria)
    kept = [p for p in parts if not p.strip().lower().startswith(_NEGATION_PREFIXES)]
    return " ".join(kept) if kept else acceptance_criteria


def pytest_target_from_ac(acceptance_criteria: str) -> str | None:
    """Resolve pytest module path from acceptance criteria prose."""
    scoped = _positive_clauses(acceptance_criteria)

    for token in scoped.split():
        if token.startswith("tests/") and token.endswith(".py"):
            return token

    lowered = scoped.lower()
    for target in STORY_TEST_TARGETS.values():
        if target.replace("tests/", "") in lowered or target in lowered:
            return target
    if "test_ferry_queue" in lowered:
        return STORY_TEST_TARGETS["queue"]
    if "test_ferry_retry" in lowered:
        return STORY_TEST_TARGETS["retry"]
    if "test_notify" in lowered or "notification" in lowered:
        return STORY_TEST_TARGETS["notify"]

    # Fall back to the story subject mentioned earliest in the positive prose.
    hits = [(lowered.find(kw), target) for kw, target in _STORY_KEYWORDS if kw in lowered]
    return min(hits)[1] if hits else None
