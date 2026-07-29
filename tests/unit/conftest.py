from __future__ import annotations

import subprocess
from contextlib import ExitStack, contextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from tests.helpers.ticket_fixtures import greeter_ticket


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


@contextmanager
def _patched_settings(overrides: dict):
    """Override Settings fields everywhere they are read, for the duration of the block.

    Two traps this walks around:

    - Patching one importing module (this used to name `graph.nodes.flow.get_settings`)
      covers that module only, so an override for a field read elsewhere — max_coverage_rounds,
      coder_step_mode — silently did nothing and the test passed regardless.
    - Patching `sprint_crew.config.get_settings` covers *nothing*: every consumer does
      `from sprint_crew.config import get_settings`, which binds the function object at
      import time and never looks at the config module again.

    So rebind the name in each module that already imported it. Unknown keys raise: a typo'd
    field on a stub is indistinguishable from a real one, which is how an override can look
    applied while changing nothing.
    """
    import sys

    from sprint_crew.config import Settings, get_settings

    unknown = set(overrides) - set(Settings.model_fields)
    if unknown:
        raise AssertionError(f"settings_overrides names no such Settings field: {sorted(unknown)}")

    real = get_settings()
    # Computed properties are not in model_fields but callers read them the same way, so
    # the stub has to carry them or it fails as an incomplete Settings rather than a
    # configured one.
    computed = {
        name
        for name, attr in vars(Settings).items()
        if isinstance(attr, property) and not name.startswith("_")
    }
    stub = SimpleNamespace(
        **{field: getattr(real, field) for field in Settings.model_fields}
        | {name: getattr(real, name) for name in computed}
        | overrides
    )
    settings_mock = MagicMock(return_value=stub)

    targets = [
        name
        for name, module in list(sys.modules.items())
        if name.startswith("sprint_crew.") and getattr(module, "get_settings", None) is get_settings
    ]
    with ExitStack() as stack:
        for name in targets:
            stack.enter_context(patch(f"{name}.get_settings", settings_mock))
        yield settings_mock


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
                    patch("sprint_crew.graph.lanes.ensure_lane", new=AsyncMock())
                ),
                "stop_lane": stack.enter_context(
                    patch("sprint_crew.graph.lanes.stop_lane", new=AsyncMock())
                ),
                "coder": stack.enter_context(
                    patch(
                        "sprint_crew.agents.coder_coverage.run_coder_with_coverage",
                        new=AsyncMock(return_value=coder_result),
                    )
                ),
                "diff": stack.enter_context(
                    patch(
                        "sprint_crew.orchestrator.workspace_diff.gather_workspace_diff",
                        return_value=workspace_diff,
                    )
                ),
                "formatter": stack.enter_context(
                    patch(
                        "sprint_crew.agents.formatter.run_formatter",
                        new=AsyncMock(return_value=formatter_result),
                    )
                ),
                "reviewer": stack.enter_context(
                    patch("sprint_crew.agents.reviewer.run_reviewer", new=reviewer)
                ),
                # Patch the git wrapper, not subprocess itself: plan_coverage delegates to
                # git_exec.git_output, so reaching through to subprocess would pin an
                # implementation detail one layer too deep.
                "git_output": stack.enter_context(
                    patch("sprint_crew.orchestrator.plan_coverage.git_output", return_value="")
                ),
                # Silenced by accident until recently: patching plan_coverage.subprocess.run
                # resolved to the *global* subprocess module, so it stubbed every subprocess
                # in the process — real git and real pytest runs included. AsyncMock because
                # run_acceptance_tests is natively async now (sprint_crew.proc).
                "acceptance_tests": stack.enter_context(
                    patch(
                        "sprint_crew.orchestrator.acceptance_tests.run_acceptance_tests",
                        new=AsyncMock(return_value=("collected 0 items", True)),
                    )
                ),
            }
            if tech_lead_result is not None:
                mocks["tech_lead"] = stack.enter_context(
                    patch(
                        "sprint_crew.agents.tech_lead_planning.run_tech_lead_validated",
                        new=AsyncMock(return_value=tech_lead_result),
                    )
                )
            if should_invoke_tester is not None:
                mocks["should_invoke_tester"] = stack.enter_context(
                    patch(
                        "sprint_crew.orchestrator.plan_coverage.should_invoke_tester",
                        return_value=should_invoke_tester,
                    )
                )
            if settings_overrides is not None:
                mocks["settings"] = stack.enter_context(_patched_settings(settings_overrides))
            yield mocks

    return _mocks


@pytest.fixture
def console_client(auth_headers: dict[str, str], tmp_path, monkeypatch):
    """TestClient with a clean, tmp-backed console store.

    Console sessions are SQLite-backed (roadmap M1); redirect the store DB and
    workspace tree to tmp so the suite never touches the real home paths. Carries
    the test bearer so requests pass the auth gate (pinned by the autouse
    isolate_unit_tests_from_services fixture).
    """
    from fastapi.testclient import TestClient

    from sprint_crew.api import console as console_module
    from sprint_crew.api.app import app
    from sprint_crew.config import get_settings
    from sprint_crew.orchestrator.run_registry import reset_run_registry

    monkeypatch.setenv("SPRINT_SESSION_DB", str(tmp_path / "console.db"))
    monkeypatch.setenv("SPRINT_WORKSPACE_BASE", str(tmp_path / "workspaces"))
    get_settings.cache_clear()
    console_module.reset_console_store()
    reset_run_registry()
    yield TestClient(app, headers=auth_headers)
    reset_run_registry()
    console_module.reset_console_store()
    get_settings.cache_clear()
