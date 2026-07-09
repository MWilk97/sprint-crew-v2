from __future__ import annotations

import time
import urllib.error
import urllib.request

_VECTOR_HEALTH_TIMEOUT_SECONDS = 300


def wait_vector_healthy(
    *,
    qdrant_url: str = "http://127.0.0.1:6333",
    embed_url: str = "http://127.0.0.1:8080",
    timeout_s: float = _VECTOR_HEALTH_TIMEOUT_SECONDS,
    interval_s: float = 5,
) -> None:
    """Block until Qdrant and embed sidecar health endpoints return 200."""
    endpoints = [
        f"{qdrant_url.rstrip('/')}/healthz",
        f"{embed_url.rstrip('/')}/health",
    ]
    deadline = time.monotonic() + timeout_s
    last_error = "unknown"
    while time.monotonic() < deadline:
        try:
            for url in endpoints:
                with urllib.request.urlopen(url, timeout=3) as resp:
                    if resp.status != 200:
                        raise urllib.error.URLError(f"HTTP {resp.status} from {url}")
            return
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            last_error = str(exc)
        time.sleep(interval_s)
    raise TimeoutError(f"Vector stack not healthy after {timeout_s}s (last: {last_error})")
