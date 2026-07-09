from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from sprint_crew.orchestrator.plan_validation import (
    PlanPathValidationError,
    snapshot_baseline_paths,
    validate_plan_paths_exist,
)
from sprint_crew.schemas.ticket import PlanStep, TaskPlan


def _init_repo(tmp_path: Path) -> Path:
    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
    return tmp_path


def test_snapshot_baseline_paths_lists_indexable_files(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    (repo / "src").mkdir()
    (repo / "src" / "ferry.py").write_text('"""Ferry."""\n', encoding="utf-8")
    (repo / "README.md").write_text("# App\n", encoding="utf-8")

    baseline = snapshot_baseline_paths(repo)
    assert "src/ferry.py" in baseline
    assert "README.md" in baseline


def test_validate_plan_paths_exist_rejects_phantom(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    (repo / "src").mkdir()
    (repo / "src" / "messaging").mkdir()
    (repo / "src" / "messaging" / "ferry.py").write_text("pass\n", encoding="utf-8")
    baseline = snapshot_baseline_paths(repo)

    plan = TaskPlan(
        ticket_key="DEMO-1",
        summary="fix ferry",
        steps=[PlanStep(description="edit ferry", files=["src/ferry/layer.py"])],
        files_to_touch=["src/ferry/layer.py"],
        acceptance_tests=["pytest -q"],
    )
    with pytest.raises(PlanPathValidationError) as exc_info:
        validate_plan_paths_exist(repo, plan, baseline_paths=baseline)
    assert "src/ferry/layer.py" in exc_info.value.phantom_paths


def test_validate_plan_paths_exist_allows_existing_file(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    (repo / "greeter.py").write_text("pass\n", encoding="utf-8")
    baseline = snapshot_baseline_paths(repo)

    plan = TaskPlan(
        ticket_key="DEMO-1",
        summary="edit greeter",
        steps=[PlanStep(description="edit", files=["greeter.py"])],
        files_to_touch=["greeter.py"],
        acceptance_tests=["pytest -q"],
    )
    validate_plan_paths_exist(repo, plan, baseline_paths=baseline)


def test_validate_plan_paths_exist_allows_new_test_under_tests(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    tests_dir = repo / "tests"
    tests_dir.mkdir()
    baseline = snapshot_baseline_paths(repo)

    plan = TaskPlan(
        ticket_key="DEMO-1",
        summary="add test",
        steps=[PlanStep(description="add test file", files=["tests/test_new.py"])],
        files_to_touch=["tests/test_new.py"],
        acceptance_tests=["pytest -q"],
    )
    validate_plan_paths_exist(repo, plan, baseline_paths=baseline)


def test_validate_plan_paths_exist_allows_create_step_description(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    baseline = snapshot_baseline_paths(repo)

    plan = TaskPlan(
        ticket_key="DEMO-1",
        summary="new module",
        steps=[PlanStep(description="create new file greeter.py", files=["greeter.py"])],
        files_to_touch=["greeter.py"],
        acceptance_tests=["pytest -q"],
    )
    validate_plan_paths_exist(repo, plan, baseline_paths=baseline)
