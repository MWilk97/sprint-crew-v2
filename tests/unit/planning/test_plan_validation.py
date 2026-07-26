from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from sprint_crew.orchestrator.plan_validation import (
    PlanPathValidationError,
    PlanScopeValidationError,
    PlanStructureValidationError,
    snapshot_baseline_paths,
    validate_plan_paths_exist,
    validate_plan_scope_conflicts,
    validate_plan_structure,
)
from sprint_crew.schemas.ticket import JiraTicket, PlanStep, TaskPlan


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


def test_validate_plan_structure_rejects_unused_files_to_touch() -> None:
    ticket = JiraTicket(
        key="DEMO-1",
        summary="REST API queue integration with SQLite",
        status="To Do",
        issue_type="Story",
    )
    plan = TaskPlan(
        ticket_key="DEMO-1",
        summary="queue",
        steps=[PlanStep(description="edit repo", files=["src/storage/sqlite_repo.py"])],
        files_to_touch=["src/storage/sqlite_repo.py", "src/messaging/queue_worker.py"],
        acceptance_tests=["pytest -q"],
    )
    with pytest.raises(PlanStructureValidationError, match="queue_worker"):
        validate_plan_structure(plan, ticket)


def test_validate_plan_structure_rejects_baseline_test_in_steps(tmp_path: Path) -> None:
    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()
    (tests_dir / "test_ferry_retry.py").write_text("pass\n", encoding="utf-8")
    ticket = JiraTicket(
        key="SCRUM-2",
        summary="retry policy",
        status="To Do",
        issue_type="Story",
    )
    plan = TaskPlan(
        ticket_key="SCRUM-2",
        summary="retry",
        steps=[
            PlanStep(
                description="implement retry",
                files=["src/messaging/retry_policy.py"],
            ),
            PlanStep(
                description="verify tests",
                files=["tests/test_ferry_retry.py"],
            ),
        ],
        files_to_touch=["src/messaging/retry_policy.py", "tests/test_ferry_retry.py"],
        acceptance_tests=["pytest tests/test_ferry_retry.py -q"],
    )
    baseline = frozenset({"tests/test_ferry_retry.py"})
    with pytest.raises(PlanStructureValidationError, match="existing test"):
        validate_plan_structure(
            plan,
            ticket,
            workspace_root=tmp_path,
            baseline_paths=baseline,
        )


def test_validate_plan_structure_allows_tests_only_in_files_to_touch() -> None:
    ticket = JiraTicket(
        key="DEMO-1",
        summary="REST API queue integration",
        status="To Do",
        issue_type="Story",
    )
    plan = TaskPlan(
        ticket_key="DEMO-1",
        summary="queue",
        steps=[PlanStep(description="edit repo", files=["src/storage/sqlite_repo.py"])],
        files_to_touch=["src/storage/sqlite_repo.py", "tests/test_ferry_queue.py"],
        acceptance_tests=["pytest -q tests/test_ferry_queue.py"],
    )
    validate_plan_structure(plan, ticket)


def test_validate_plan_structure_allows_new_test_in_steps(tmp_path: Path) -> None:
    ticket = JiraTicket(
        key="DEMO-1",
        summary="add worker tests",
        status="To Do",
        issue_type="Story",
    )
    plan = TaskPlan(
        ticket_key="DEMO-1",
        summary="worker",
        steps=[
            PlanStep(
                description="create new test file",
                files=["src/worker.py", "tests/test_worker.py"],
            )
        ],
        files_to_touch=["src/worker.py", "tests/test_worker.py"],
        acceptance_tests=["pytest tests/test_worker.py -q"],
    )
    validate_plan_structure(
        plan,
        ticket,
        workspace_root=tmp_path,
        baseline_paths=frozenset(),
    )


def test_validate_plan_scope_conflicts_rejects_overlap() -> None:
    plan = TaskPlan(
        ticket_key="DEMO-1",
        summary="queue",
        steps=[
            PlanStep(
                description="wire queue worker",
                files=["src/messaging/queue_worker.py", "src/storage/sqlite_repo.py"],
            )
        ],
        files_to_touch=["src/messaging/queue_worker.py", "src/storage/sqlite_repo.py"],
        out_of_scope=["src/storage/sqlite_repo.py"],
        acceptance_tests=["pytest -q tests/test_ferry_queue.py"],
    )
    with pytest.raises(PlanScopeValidationError, match="sqlite_repo"):
        validate_plan_scope_conflicts(plan)


def test_validate_plan_scope_conflicts_allows_disjoint_scope() -> None:
    plan = TaskPlan(
        ticket_key="DEMO-1",
        summary="queue",
        steps=[PlanStep(description="edit worker", files=["src/messaging/queue_worker.py"])],
        files_to_touch=["src/messaging/queue_worker.py"],
        out_of_scope=["src/messaging/retry_policy.py"],
        acceptance_tests=["pytest -q"],
    )
    validate_plan_scope_conflicts(plan)
