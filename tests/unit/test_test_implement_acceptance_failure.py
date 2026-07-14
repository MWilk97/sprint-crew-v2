from __future__ import annotations

from unittest.mock import patch

import pytest
from tests.unit.test_acceptance_failure import SCRUM3_COLLECTION

from sprint_crew.schemas.change import CodeChange
from sprint_crew.schemas.ticket import TaskPlan

pytestmark = pytest.mark.asyncio


async def test_test_implement_records_acceptance_failure_when_collection_fails(
    base_state: dict,
    task_plan: TaskPlan,
    code_change: CodeChange,
) -> None:
    from sprint_crew.graph.pipeline import test_implement

    base_state["task_plan"] = task_plan.model_dump()
    base_state["code_change"] = code_change.model_dump()

    with (
        patch(
            "sprint_crew.graph.pipeline.run_acceptance_tests",
            return_value=(SCRUM3_COLLECTION, False),
        ),
        patch("sprint_crew.graph.pipeline.should_invoke_tester", return_value=False),
    ):
        result = await test_implement(base_state)  # type: ignore[arg-type]

    assert result["acceptance_failure"]
    assert result["acceptance_failure"]["kind"] != "none"


async def test_test_implement_clears_stale_acceptance_failure_once_tests_are_green(
    base_state: dict,
    task_plan: TaskPlan,
    code_change: CodeChange,
) -> None:
    """Regression: a failure recorded in an earlier retry round must not leak into
    a later round's feedback once tests are green again — acceptance_failure has no
    LangGraph reducer, so the node must explicitly clear it every round."""
    from sprint_crew.graph.pipeline import test_implement

    base_state["task_plan"] = task_plan.model_dump()
    base_state["code_change"] = code_change.model_dump()
    base_state["acceptance_failure"] = {
        "kind": "collection_error",
        "tester_can_help": False,
        "source_paths": ["src/api/routes.py"],
        "test_paths": [],
        "summary": "stale failure from a prior round",
        "detail_excerpt": "",
    }

    with (
        patch(
            "sprint_crew.graph.pipeline.run_acceptance_tests",
            return_value=("exit_code=0", True),
        ),
        patch("sprint_crew.graph.pipeline.should_invoke_tester", return_value=False),
    ):
        result = await test_implement(base_state)  # type: ignore[arg-type]

    assert result["acceptance_failure"] == {}
