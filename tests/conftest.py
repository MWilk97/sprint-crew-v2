from __future__ import annotations

import os
from pathlib import Path

import pytest

from sprint_crew.config import get_settings
from sprint_crew.schemas.change import CodeChange, ReviewOutcome
from sprint_crew.schemas.ticket import JiraTicket, PlanStep, TaskPlan
from tests.helpers.vector_ab import copy_fixture_workspace


def skip_unless_env(var: str, reason: str) -> None:
    if os.environ.get(var, "").strip() not in {"1", "true", "yes"}:
        pytest.skip(f"{reason} (set {var}=1)")


@pytest.fixture(autouse=True)
def disable_vector_index_for_unit_tests(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Keep unit/CI tests free of Qdrant unless explicitly marked vector_live / vector_agent_live."""
    if request.node.get_closest_marker("vector_live") or request.node.get_closest_marker(
        "vector_agent_live"
    ):
        return
    monkeypatch.setenv("VECTOR_INDEX_ENABLED", "false")
    get_settings.cache_clear()


@pytest.fixture
def sample_ticket() -> JiraTicket:
    return JiraTicket(
        key="DEMO-1",
        summary="Add hello() to greeter module",
        description="Implement hello() returning 'hello'.",
        status="To Do",
        issue_type="Story",
        acceptance_criteria="- Unit tests pass\n- hello() returns 'hello'",
    )


@pytest.fixture
def task_plan() -> TaskPlan:
    return TaskPlan(
        ticket_key="DEMO-1",
        summary="Add hello()",
        steps=[PlanStep(description="edit greeter", files=["greeter.py"])],
        acceptance_tests=["pytest -q"],
    )


@pytest.fixture
def code_change() -> CodeChange:
    return CodeChange(
        ticket_key="DEMO-1",
        branch="feature/demo-1",
        summary="added hello",
        tests_passed=True,
    )


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


@pytest.fixture
def skip_unless_integration_live() -> None:
    skip_unless_env("INTEGRATION_LIVE", "integration live tests require sandbox credentials")


@pytest.fixture
def skip_unless_vector_live() -> None:
    skip_unless_env("VECTOR_LIVE", "vector live tests require Qdrant stack")


@pytest.fixture
def skip_unless_preflight_live() -> None:
    skip_unless_env("PREFLIGHT_LIVE", "preflight tests require live vLLM lanes")
