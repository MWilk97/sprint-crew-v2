"""Generic HTTP retry utilities (decoy — not the messaging retry policy)."""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import TypeVar

T = TypeVar("T")


def retry_http(
    fn: Callable[[], T],
    *,
    max_attempts: int = 5,
    delay_seconds: float = 1.0,
) -> T:
    """Retry an HTTP client call with fixed delay between delivery attempts."""
    last_exc: Exception | None = None
    for attempt in range(max_attempts):
        try:
            return fn()
        except Exception as exc:
            last_exc = exc
            if attempt < max_attempts - 1:
                time.sleep(delay_seconds)
    assert last_exc is not None
    raise last_exc
