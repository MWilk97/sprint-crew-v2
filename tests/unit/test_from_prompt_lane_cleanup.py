from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from fastapi import BackgroundTasks

from sprint_crew.api.app import FromPromptRequest, sprint_from_prompt


@pytest.mark.asyncio
async def test_sprint_from_prompt_stops_work_lane_when_scrum_master_fails(tmp_path) -> None:
    """Regression: ensure_lane/stop_lane must bracket run_scrum_master with try/finally —
    otherwise an exception mid-call leaves the Work lane loaded (GX10 unified-memory risk,
    see AGENTS.md 4.1: never keep 2+ vLLM lanes loaded at once)."""
    stop_mock = AsyncMock()
    with (
        patch("sprint_crew.api.app.prepare_workspace", return_value=tmp_path),
        patch("sprint_crew.api.app.maybe_index_workspace"),
        patch("sprint_crew.api.app.enrich_repo_context", return_value=""),
        patch("sprint_crew.api.app.ensure_lane", new=AsyncMock()),
        patch("sprint_crew.api.app.stop_lane", new=stop_mock),
        patch(
            "sprint_crew.api.app.run_scrum_master",
            new=AsyncMock(side_effect=RuntimeError("boom")),
        ),
    ):
        with pytest.raises(RuntimeError):
            await sprint_from_prompt(FromPromptRequest(prompt="add a feature"), BackgroundTasks())

    stop_mock.assert_awaited_once()
