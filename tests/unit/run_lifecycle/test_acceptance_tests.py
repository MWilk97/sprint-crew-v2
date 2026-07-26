from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import patch

import pytest
from tests.helpers.ticket_fixtures import greeter_task_plan

from sprint_crew.orchestrator.acceptance_tests import (
    AcceptanceTestsValidationError,
    run_acceptance_tests,
    validate_acceptance_tests,
)
from sprint_crew.proc import ProcResult


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


@pytest.mark.asyncio
async def test_run_acceptance_tests_green_on_fixture(tmp_workspace) -> None:
    plan = greeter_task_plan()
    (tmp_workspace / "greeter.py").write_text(
        'def hello():\n    return "hello"\n',
        encoding="utf-8",
    )
    output, passed = await run_acceptance_tests(tmp_workspace, plan.acceptance_tests)
    assert passed is True
    assert "exit_code=0" in output


@pytest.mark.asyncio
async def test_run_acceptance_tests_red_when_tests_fail(tmp_workspace) -> None:
    plan = greeter_task_plan()
    (tmp_workspace / "greeter.py").write_text("# empty\n", encoding="utf-8")
    output, passed = await run_acceptance_tests(tmp_workspace, plan.acceptance_tests)
    assert passed is False
    assert "exit_code=" in output


@pytest.mark.asyncio
async def test_run_acceptance_tests_marks_timeout_as_failure(tmp_path: Path) -> None:
    # A hung acceptance command must count as a failed test, not crash the run.
    async def _timed_out(*args: object, **kwargs: object) -> ProcResult:
        return ProcResult(stdout="", stderr="", returncode=-1, timed_out=True)

    with patch("sprint_crew.orchestrator.acceptance_tests.run_shell", new=_timed_out):
        output, passed = await run_acceptance_tests(tmp_path, ["pytest -q"])
    assert passed is False
    assert "timed out" in output


@pytest.mark.asyncio
async def test_cancelling_acceptance_tests_kills_the_child(tmp_path: Path) -> None:
    """The bug this guards: asyncio.to_thread(subprocess.run) is not cancellable — the
    coroutine unwinds but the child runs to completion, so a pytest kept burning CPU for
    up to ACCEPTANCE_TEST_TIMEOUT_S after the user pressed Stop, possibly alongside the
    next run admitted into the single slot.

    The command is `sleep N; touch marker` through a shell, so it also pins the
    process-group kill: signalling only the shell's pid would leave the sleep orphaned and
    the marker would still appear.
    """
    marker = tmp_path / "child-survived"

    child_lifetime = 2.0

    task = asyncio.create_task(
        run_acceptance_tests(tmp_path, [f"sleep {child_lifetime}; touch {marker}"])
    )
    await asyncio.sleep(0.3)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    # Must outlast the child, or the assertion is vacuous: an orphan that has not yet
    # reached its `touch` looks identical to one that was killed.
    await asyncio.sleep(child_lifetime)
    assert not marker.exists(), "acceptance child outlived the cancelled run"
