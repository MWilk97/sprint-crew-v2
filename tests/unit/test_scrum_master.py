from __future__ import annotations

from unittest.mock import patch

import pytest

from sprint_crew.agents.scrum_master import run_scrum_master
from sprint_crew.config import Role
from sprint_crew.schemas.backlog import BacklogPlan, BacklogStory, ProductBrief


@pytest.mark.asyncio
async def test_scrum_master_returns_valid_backlog_plan_schema() -> None:
    plan = BacklogPlan(
        product_brief=ProductBrief(title="Greeter", summary="Add hello"),
        stories=[BacklogStory(key="STORY-1", summary="Add hello() to greeter.py")],
        recommended_first="STORY-1",
    )
    with patch(
        "sprint_crew.agents.scrum_master.structured_completion",
        return_value=plan,
    ) as completion_mock:
        result = await run_scrum_master(user_prompt="Add hello() to greeter.py with pytest.")

    completion_mock.assert_called_once()
    assert completion_mock.call_args.args[0] == Role.WORK
    assert len(result.stories) >= 1
    assert result.recommended_first
