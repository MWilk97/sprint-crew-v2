from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from tests.helpers.vector_fixtures import vector_fixture_root
from tests.helpers.vector_tiers import trap_strict_mode, write_trap_report

_ROOT = Path(__file__).resolve().parents[3]
_PYTEST_BIN = _ROOT / ".venv" / "bin" / "pytest"


@pytest.mark.agent_trap
def test_trap_stdlib_shadow_fixture_collection_error() -> None:
    """Unit-speed trap: stdlib platform shadow on vector_repo notify tests (no GPU)."""
    fixture_root = vector_fixture_root()
    proc = subprocess.run(
        [str(_PYTEST_BIN), "--collect-only", "-q", "tests/test_notify_routes.py"],
        cwd=fixture_root,
        capture_output=True,
        text=True,
        check=False,
    )
    report = {
        "tier": "trap",
        "trap": "stdlib_shadow_vector_repo",
        "collect_exit_code": proc.returncode,
        "stdout": proc.stdout[-2000:],
        "stderr": proc.stderr[-2000:],
        "expected": "collection_error",
    }
    path = write_trap_report(report)
    print(f"trap stdlib shadow collect report: {path}")

    if trap_strict_mode():
        assert proc.returncode != 0, "expected collection error on base vector_repo"
    elif proc.returncode == 0:
        print("note: trap fixture unexpectedly collects — shadow may be environment-specific")
