from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest
from tests.helpers.ticket_fixtures import greeter_task_plan

from sprint_crew.orchestrator.acceptance_tests import (
    AcceptanceTestsValidationError,
    run_acceptance_tests,
    validate_acceptance_tests,
)


def test_validate_acceptance_tests_accepts_pytest_q() -> None:
    assert validate_acceptance_tests(["pytest -q"]) == ["pytest -q"]


def test_validate_acceptance_tests_accepts_pytest_k_passes() -> None:
    assert validate_acceptance_tests(["pytest -k passes"]) == ["pytest -k passes"]


def test_validate_acceptance_tests_rejects_prose_suffix() -> None:
    with pytest.raises(AcceptanceTestsValidationError) as exc:
        validate_acceptance_tests(["pytest -q passes"])
    assert "pytest -q passes" in exc.value.invalid


def test_validate_acceptance_tests_rejects_curl() -> None:
    with pytest.raises(AcceptanceTestsValidationError):
        validate_acceptance_tests(["curl http://localhost"])


def test_validate_acceptance_tests_rejects_empty() -> None:
    with pytest.raises(AcceptanceTestsValidationError):
        validate_acceptance_tests([])


def test_run_acceptance_tests_green_on_fixture(tmp_workspace) -> None:
    plan = greeter_task_plan()
    (tmp_workspace / "greeter.py").write_text(
        'def hello():\n    return "hello"\n',
        encoding="utf-8",
    )
    output, passed = run_acceptance_tests(tmp_workspace, plan.acceptance_tests)
    assert passed is True
    assert "exit_code=0" in output


def test_run_acceptance_tests_red_when_tests_fail(tmp_workspace) -> None:
    plan = greeter_task_plan()
    (tmp_workspace / "greeter.py").write_text("# empty\n", encoding="utf-8")
    output, passed = run_acceptance_tests(tmp_workspace, plan.acceptance_tests)
    assert passed is False
    assert "exit_code=" in output


def test_run_acceptance_tests_marks_timeout_as_failure(tmp_path: Path) -> None:
    # A hung acceptance command must count as a failed test, not crash the run.
    with patch(
        "sprint_crew.orchestrator.acceptance_tests.subprocess.run",
        side_effect=subprocess.TimeoutExpired(cmd="pytest -q", timeout=900),
    ):
        output, passed = run_acceptance_tests(tmp_path, ["pytest -q"])
    assert passed is False
    assert "timed out" in output
