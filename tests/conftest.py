from __future__ import annotations

import os
from pathlib import Path

import pytest

from sprint_crew.config import get_settings
from sprint_crew.schemas.change import CodeChange, ReviewOutcome
from sprint_crew.schemas.ticket import JiraTicket, PlanStep, TaskPlan
from tests.helpers.agent_live_tickets import greeter_code_change, greeter_task_plan, greeter_ticket
from tests.helpers.vector_ab import copy_fixture_workspace


def skip_unless_env(var: str, reason: str) -> None:
    if os.environ.get(var, "").strip() not in {"1", "true", "yes"}:
        pytest.skip(f"{reason} (set {var}=1)")


@pytest.fixture(autouse=True)
def disable_vector_index_for_unit_tests(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Keep unit/CI tests free of Qdrant unless explicitly marked agent_live."""
    if request.node.get_closest_marker("agent_live"):
        return
    monkeypatch.setenv("VECTOR_INDEX_ENABLED", "false")
    get_settings.cache_clear()


@pytest.fixture
def sample_ticket() -> JiraTicket:
    return greeter_ticket()


@pytest.fixture
def task_plan() -> TaskPlan:
    return greeter_task_plan(
        summary="Add hello()",
        steps=[PlanStep(description="edit greeter", files=["greeter.py"])],
        files_to_touch=["greeter.py"],
        acceptance_tests=["pytest -q"],
    )


@pytest.fixture
def code_change() -> CodeChange:
    return greeter_code_change()


@pytest.fixture
def passing_review() -> ReviewOutcome:
    return ReviewOutcome(
        ticket_key="DEMO-1",
        passed=True,
        summary="ok",
        tests_passed=True,
    )


@pytest.fixture
def api_db(tmp_path, monkeypatch: pytest.MonkeyPatch):
    db_path = tmp_path / "api-routes.db"
    monkeypatch.setenv("SPRINT_SESSION_DB", str(db_path))
    get_settings.cache_clear()
    yield db_path
    get_settings.cache_clear()


@pytest.fixture
def fixture_repo_path() -> Path:
    return get_settings().project_root / "fixtures" / "repo"


@pytest.fixture
def fixture_vector_repo_path() -> Path:
    return get_settings().project_root / "fixtures" / "vector_repo"


@pytest.fixture
def tmp_workspace(fixture_repo_path: Path, tmp_path: Path) -> Path:
    """Copy greeter fixture into tmp_path and init git."""
    return copy_fixture_workspace(fixture_repo_path, tmp_path, name="workspace")


@pytest.fixture
def skip_unless_vllm_live() -> None:
    skip_unless_env("VLLM_LIVE", "vLLM live tests require GPU lanes")
