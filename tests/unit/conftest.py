from __future__ import annotations

import subprocess
from contextlib import ExitStack, contextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from tests.helpers.agent_live_tickets import greeter_ticket


@pytest.fixture
def satisfied_coder_result() -> tuple:
    """`run_coder_with_coverage` return value: handoff, empty tool log, coverage satisfied.

    Built per test — the MagicMock caches auto-created children (`phantom_paths`,
    `blocking_unexpected`), so a shared instance would leak them across tests.
    """
    return (
        "handoff",
        [],
        MagicMock(satisfied=True, missing=[], unexpected=[], out_of_scope_hits=[]),
        "",
        False,
    )


@pytest.fixture
def base_state(tmp_path) -> dict:
    # init_session rejects a non-repo workspace, and the ship node commits for real.
    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
    ticket = greeter_ticket(acceptance_criteria="pytest -q")
    return {
        "session_id": "test-session",
        "workspace_root": str(tmp_path),
        "selected_ticket": ticket.model_dump(),
        "attempt": 0,
        "prior_review_feedback": "",
        "events": [],
        "use_real_ship": False,
    }


@pytest.fixture
def graph_run_mocks(satisfied_coder_result: tuple):
    """Factory for the common build_sprint_graph().ainvoke(...) mock bundle used by
    tests/unit/test_graph_pipeline.py.

    Pass tech_lead_result=None (the default) to leave run_tech_lead_validated
    unpatched, e.g. to exercise the real template fast-path. Pass
    should_invoke_tester=None (the default) to leave it unpatched as well.
    """

    @contextmanager
    def _mocks(
        *,
        reviewer,
        formatter_result,
        coder_result=satisfied_coder_result,
        tech_lead_result=None,
        workspace_diff: str = "diff",
        should_invoke_tester: bool | None = None,
        settings_overrides: dict | None = None,
    ):
        with ExitStack() as stack:
            mocks: dict[str, object] = {
                "ensure_lane": stack.enter_context(
                    patch("sprint_crew.graph.pipeline.ensure_lane", new=AsyncMock())
                ),
                "stop_lane": stack.enter_context(
                    patch("sprint_crew.graph.pipeline.stop_lane", new=AsyncMock())
                ),
                "coder": stack.enter_context(
                    patch(
                        "sprint_crew.graph.pipeline.run_coder_with_coverage",
                        new=AsyncMock(return_value=coder_result),
                    )
                ),
                "diff": stack.enter_context(
                    patch(
                        "sprint_crew.graph.pipeline.gather_workspace_diff",
                        return_value=workspace_diff,
                    )
                ),
                "formatter": stack.enter_context(
                    patch(
                        "sprint_crew.graph.pipeline.run_formatter",
                        new=AsyncMock(return_value=formatter_result),
                    )
                ),
                "reviewer": stack.enter_context(
                    patch("sprint_crew.graph.pipeline.run_reviewer", new=reviewer)
                ),
                "subprocess": stack.enter_context(
                    patch("sprint_crew.orchestrator.plan_coverage.subprocess.run")
                ),
            }
            mocks["subprocess"].return_value.returncode = 0
            mocks["subprocess"].return_value.stdout = ""
            mocks["subprocess"].return_value.stderr = ""
            if tech_lead_result is not None:
                mocks["tech_lead"] = stack.enter_context(
                    patch(
                        "sprint_crew.graph.pipeline.run_tech_lead_validated",
                        new=AsyncMock(return_value=tech_lead_result),
                    )
                )
            if should_invoke_tester is not None:
                mocks["should_invoke_tester"] = stack.enter_context(
                    patch(
                        "sprint_crew.graph.pipeline.should_invoke_tester",
                        return_value=should_invoke_tester,
                    )
                )
            if settings_overrides is not None:
                settings_mock = stack.enter_context(
                    patch("sprint_crew.graph.pipeline.get_settings")
                )
                for key, value in settings_overrides.items():
                    setattr(settings_mock.return_value, key, value)
                mocks["settings"] = settings_mock
            yield mocks

    return _mocks


@pytest.fixture
def console_client():
    """TestClient with a clean console store — the store is module-global state."""
    from fastapi.testclient import TestClient

    from sprint_crew.api import console as console_module
    from sprint_crew.api.app import app

    console_module.reset_console_store()
    yield TestClient(app)
    console_module.reset_console_store()
