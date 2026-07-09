"""Retry policy stub — implement exponential backoff for adapter handoff failures."""

from __future__ import annotations

from collections.abc import Callable
from typing import TypeVar

T = TypeVar("T")


def with_retry(
    fn: Callable[[], T],
    *,
    max_attempts: int = 3,
    base_delay_ms: int = 100,
) -> T:
    """Run fn with retries. Not implemented — story 2."""
    raise NotImplementedError("retry policy not implemented")
