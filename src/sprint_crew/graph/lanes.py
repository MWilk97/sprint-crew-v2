from __future__ import annotations

import asyncio
import logging
import subprocess
import urllib.error
import urllib.request
from typing import Final

from sprint_crew.config import Role, get_settings, lane_for_role
from sprint_crew.orchestrator.emitter import emit_live
from sprint_crew.schemas.session import agent_event

logger = logging.getLogger(__name__)

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


def _lane_script() -> str:
    return str(get_settings().project_root / "scripts" / "lane-ctl.sh")


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


def lane_status(role: Role) -> str:
    url = _health_url(role)
    try:
        with urllib.request.urlopen(url, timeout=2) as resp:
            return "ok" if resp.status == 200 else "degraded"
    except (urllib.error.URLError, TimeoutError, OSError):
        return "down"


def lane_health() -> dict[str, str]:
    """Per-lane health summary ("ok" / "degraded" / "down"), keyed by lane name.

    Probes every lane. Callers that care about one should use ``lane_status`` instead of
    paying a round-trip — and a timeout — on the lanes they discard.
    """
    return {_lane_name(role): lane_status(role) for role in Role}


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
        if await asyncio.to_thread(_lane_stopped, role):
            return
        await asyncio.sleep(_STOP_POLL_INTERVAL)
    raise TimeoutError(f"Lane {lane_name} did not stop within {timeout}s")


async def ensure_lane(role: Role) -> None:
    if await asyncio.to_thread(_lane_healthy, role):
        return
    lane_name = _lane_name(role)
    # Emit before the (minutes-long) stop/start/health-poll block, so the UI shows a
    # "loading" state up front rather than a silent gap. The matching lane_ready is
    # emitted on every exit path, including failures — a UI keying a spinner on
    # loading→ready would otherwise spin forever when a lane fails to come up.
    emit_live(
        agent_event(
            "orchestrator",
            "lane_loading",
            f"Loading lane {lane_name}",
            lane=lane_name,
            budget_s=_HEALTH_TIMEOUT_SECONDS,
        )
    )
    started = asyncio.get_event_loop().time()

    def _ready_event(*, ok: bool, error: str | None = None) -> None:
        emit_live(
            agent_event(
                "orchestrator",
                "lane_ready",
                f"Lane {lane_name} ready" if ok else f"Lane {lane_name} failed to load",
                level="info" if ok else "error",
                lane=lane_name,
                ok=ok,
                error=error,
                duration_ms=round((asyncio.get_event_loop().time() - started) * 1000, 1),
            )
        )

    try:
        # Dedupe by lane, not by Role: _ROLE_TO_LANE is many-to-one, so two roles sharing a
        # lane would otherwise make this stop the container it is about to start.
        for other in Role:
            if _lane_name(other) != _lane_name(role):
                await stop_lane(other)
        proc = await asyncio.create_subprocess_exec(
            _lane_script(),
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
            if await asyncio.to_thread(_lane_healthy, role):
                _ready_event(ok=True)
                return
            await asyncio.sleep(_HEALTH_POLL_INTERVAL)
        raise TimeoutError(
            f"Lane {lane_name} did not become healthy within {_HEALTH_TIMEOUT_SECONDS}s"
        )
    except BaseException as exc:
        _ready_event(ok=False, error=str(exc)[:200])
        raise


async def stop_all_lanes() -> None:
    """Best-effort teardown of every lane. Never raises — callers are cleanup paths.

    Idempotent (``stop_lane`` early-returns on an already-stopped lane), which is what lets
    the RunRegistry repeat it after a run's own ``finally`` may have been interrupted.
    """
    for role in Role:
        try:
            await stop_lane(role)
        except Exception:
            logger.warning("Failed to stop lane %s during cleanup", role, exc_info=True)


async def stop_lane(role: Role) -> None:
    """Stop a lane and wait until health is down and container is not running."""
    lane_name = _lane_name(role)
    if await asyncio.to_thread(_lane_stopped, role):
        return
    proc = await asyncio.create_subprocess_exec(
        _lane_script(),
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
    emit_live(
        agent_event("orchestrator", "lane_stopped", f"Lane {lane_name} stopped", lane=lane_name)
    )
