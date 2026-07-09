from __future__ import annotations

import subprocess
import time
import urllib.error
import urllib.request


def docker_available() -> bool:
    return subprocess.run(["docker", "info"], capture_output=True, check=False).returncode == 0


# Match sprint_crew.graph.lanes._HEALTH_TIMEOUT_SECONDS (20 min).
_LANE_HEALTH_TIMEOUT_SECONDS = 1200


def wait_lane_healthy(
    url: str,
    *,
    timeout_s: float = _LANE_HEALTH_TIMEOUT_SECONDS,
    interval_s: float = 5,
) -> None:
    """Block until lane /health returns 200 or raise TimeoutError."""
    deadline = time.monotonic() + timeout_s
    last_error = "unknown"
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=3) as resp:
                if resp.status == 200:
                    return
                last_error = f"HTTP {resp.status}"
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            last_error = str(exc)
        time.sleep(interval_s)
    raise TimeoutError(f"Lane not healthy at {url} after {timeout_s}s (last: {last_error})")
