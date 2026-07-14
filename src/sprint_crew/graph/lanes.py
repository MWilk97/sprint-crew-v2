from __future__ import annotations

import asyncio
import subprocess
import urllib.error
import urllib.request
from typing import Final

from sprint_crew.config import Role, get_settings, lane_for_role

_LANE_SCRIPT: Final = get_settings().project_root / "scripts" / "lane-ctl.sh"
_HEALTH_TIMEOUT_SECONDS: Final = 1200
_STOP_TIMEOUT_SECONDS: Final = 180
_HEALTH_POLL_INTERVAL: Final = 5.0
_STOP_POLL_INTERVAL: Final = 2.0

_ROLE_TO_LANE: Final = {
    Role.CODING: "coder",
    Role.WORK: "work",
}


def _lane_name(role: Role) -> str:
    return _ROLE_TO_LANE[role]


def _container_name(role: Role) -> str:
    service = {"coder": "vllm-coder", "work": "vllm-work"}[_lane_name(role)]
    return f"infra-{service}-1"


def _health_url(role: Role) -> str:
    lane = lane_for_role(role)
    base = lane.base_url.rstrip("/")
    if base.endswith("/v1"):
        base = base[:-3]
    return f"{base}/health"


def _lane_healthy(role: Role) -> bool:
    url = _health_url(role)
    try:
        with urllib.request.urlopen(url, timeout=3) as resp:
            return resp.status == 200
    except (urllib.error.URLError, TimeoutError, OSError):
        return False


def _lane_status(role: Role) -> str:
    url = _health_url(role)
    try:
        with urllib.request.urlopen(url, timeout=2) as resp:
            return "ok" if resp.status == 200 else "degraded"
    except (urllib.error.URLError, TimeoutError, OSError):
        return "down"


def lane_health() -> dict[str, str]:
    """Per-lane health summary ("ok" / "degraded" / "down"), keyed by lane name."""
    return {_lane_name(role): _lane_status(role) for role in Role}


def _lane_container_running(role: Role) -> bool:
    proc = subprocess.run(
        ["docker", "inspect", "-f", "{{.State.Status}}", _container_name(role)],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        return False
    return proc.stdout.strip() == "running"


def _lane_stopped(role: Role) -> bool:
    return not _lane_healthy(role) and not _lane_container_running(role)


async def wait_lane_stopped(role: Role, *, timeout: float = _STOP_TIMEOUT_SECONDS) -> None:
    """Poll until lane health is down and container is not running."""
    lane_name = _lane_name(role)
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        if _lane_stopped(role):
            return
        await asyncio.sleep(_STOP_POLL_INTERVAL)
    raise TimeoutError(f"Lane {lane_name} did not stop within {timeout}s")


async def ensure_lane(role: Role) -> None:
    if _lane_healthy(role):
        return
    for other in Role:
        if other != role:
            await stop_lane(other)
    lane_name = _lane_name(role)
    proc = await asyncio.create_subprocess_exec(
        str(_LANE_SCRIPT),
        "start",
        lane_name,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    if proc.returncode != 0:
        msg = stderr.decode() or stdout.decode() or f"lane-ctl start {lane_name} failed"
        raise RuntimeError(msg)

    deadline = asyncio.get_event_loop().time() + _HEALTH_TIMEOUT_SECONDS
    while asyncio.get_event_loop().time() < deadline:
        if _lane_healthy(role):
            return
        await asyncio.sleep(_HEALTH_POLL_INTERVAL)
    raise TimeoutError(f"Lane {lane_name} did not become healthy within {_HEALTH_TIMEOUT_SECONDS}s")


async def stop_lane(role: Role) -> None:
    """Stop a lane and wait until health is down and container is not running."""
    lane_name = _lane_name(role)
    if _lane_stopped(role):
        return
    proc = await asyncio.create_subprocess_exec(
        str(_LANE_SCRIPT),
        "stop",
        lane_name,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    if proc.returncode != 0:
        msg = stderr.decode() or stdout.decode() or f"lane-ctl stop {lane_name} failed"
        raise RuntimeError(msg)
    await wait_lane_stopped(role)
