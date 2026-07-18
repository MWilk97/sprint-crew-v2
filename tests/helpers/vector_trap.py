from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def pytest_collect_only(cwd: Path, target: str) -> subprocess.CompletedProcess[str]:
    """Run pytest --collect-only against a target under cwd (uses the current interpreter)."""
    return subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q", target],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
    )


def collect_notify_tests(fixture_root: Path) -> subprocess.CompletedProcess[str]:
    """Collect notify route tests — baseline vector_repo should fail (stdlib shadow)."""
    return pytest_collect_only(fixture_root, "tests/test_notify_routes.py")


def assert_notify_tests_fail_collection(proc: subprocess.CompletedProcess[str]) -> None:
    assert proc.returncode != 0, "expected collection error on base vector_repo"
    combined = (proc.stdout + proc.stderr).lower()
    assert "error" in combined or "platform" in combined


def stdlib_shadow_trap_report(proc: subprocess.CompletedProcess[str]) -> dict:
    return {
        "tier": "trap",
        "trap": "stdlib_shadow_vector_repo",
        "collect_exit_code": proc.returncode,
        "stdout": proc.stdout[-2000:],
        "stderr": proc.stderr[-2000:],
        "expected": "collection_error",
    }
