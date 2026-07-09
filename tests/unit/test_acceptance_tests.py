from __future__ import annotations

import pytest

from sprint_crew.orchestrator.acceptance_tests import (
    AcceptanceTestsValidationError,
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
