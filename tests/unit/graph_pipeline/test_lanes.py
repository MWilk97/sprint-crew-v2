from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from sprint_crew.config import Role
from sprint_crew.graph import lanes


@pytest.mark.asyncio
async def test_stop_lane_noop_when_already_stopped() -> None:
    with (
        patch.object(lanes, "_lane_stopped", return_value=True),
        patch.object(lanes, "wait_lane_stopped", new=AsyncMock()) as wait_mock,
    ):
        await lanes.stop_lane(Role.CODING)
    wait_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_stop_lane_waits_until_stopped() -> None:
    communicate = AsyncMock(return_value=(b"", b""))
    proc = MagicMock()
    proc.communicate = communicate
    proc.returncode = 0

    with (
        patch.object(lanes, "_lane_stopped", side_effect=[False, False, True]),
        patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=proc)),
        patch.object(lanes, "wait_lane_stopped", new=AsyncMock()) as wait_mock,
    ):
        await lanes.stop_lane(Role.WORK)

    wait_mock.assert_awaited_once_with(Role.WORK)


@pytest.mark.asyncio
async def test_wait_lane_stopped_returns_when_healthy_and_container_down() -> None:
    with (
        patch.object(lanes, "_lane_stopped", side_effect=[False, True]),
        patch("asyncio.sleep", new=AsyncMock()),
    ):
        await lanes.wait_lane_stopped(Role.CODING, timeout=10)


@pytest.mark.asyncio
async def test_wait_lane_stopped_raises_on_timeout() -> None:
    with (
        patch.object(lanes, "_lane_stopped", return_value=False),
        patch("asyncio.sleep", new=AsyncMock()),
        patch.object(lanes.asyncio.get_event_loop(), "time", side_effect=[0.0, 200.0]),
    ):
        with pytest.raises(TimeoutError, match="did not stop"):
            await lanes.wait_lane_stopped(Role.WORK, timeout=180)


@pytest.mark.asyncio
async def test_ensure_lane_stops_other_lanes_before_start() -> None:
    stop_mock = AsyncMock()
    with (
        patch.object(lanes, "_lane_healthy", return_value=False),
        patch.object(lanes, "stop_lane", new=stop_mock),
        patch("asyncio.create_subprocess_exec", new=AsyncMock()) as exec_mock,
        patch.object(lanes, "wait_lane_stopped", new=AsyncMock()),
    ):
        proc = MagicMock()
        proc.communicate = AsyncMock(return_value=(b"", b""))
        proc.returncode = 0
        exec_mock.return_value = proc
        with patch.object(lanes, "_lane_healthy", side_effect=[False, True]):
            await lanes.ensure_lane(Role.CODING)

    stopped_roles = {call.args[0] for call in stop_mock.await_args_list}
    assert Role.WORK in stopped_roles
    assert Role.CODING not in stopped_roles
