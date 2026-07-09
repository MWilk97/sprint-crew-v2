"""Story 2 — exponential backoff retry on adapter handoff failure."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from messaging.retry_policy import with_retry


def test_with_retry_succeeds_after_transient_failure() -> None:
    fn = MagicMock(side_effect=[RuntimeError("handoff failed"), RuntimeError("again"), "ok"])
    result = with_retry(fn, max_attempts=3, base_delay_ms=1)
    assert result == "ok"
    assert fn.call_count == 3


def test_with_retry_raises_after_max_attempts() -> None:
    fn = MagicMock(side_effect=RuntimeError("handoff failed"))
    with pytest.raises(RuntimeError):
        with_retry(fn, max_attempts=3, base_delay_ms=1)
    assert fn.call_count == 3
