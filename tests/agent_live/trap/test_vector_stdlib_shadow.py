from __future__ import annotations

import pytest

from tests.helpers.vector_fixtures import vector_fixture_root
from tests.helpers.vector_tiers import trap_strict_mode, write_trap_report
from tests.helpers.vector_trap import collect_notify_tests, stdlib_shadow_trap_report


@pytest.mark.agent_trap
def test_trap_stdlib_shadow_fixture_collection_error() -> None:
    """Unit-speed trap: stdlib platform shadow on vector_repo notify tests (no GPU)."""
    proc = collect_notify_tests(vector_fixture_root())
    report = stdlib_shadow_trap_report(proc)
    path = write_trap_report(report)
    print(f"trap stdlib shadow collect report: {path}")

    if trap_strict_mode():
        assert proc.returncode != 0, "expected collection error on base vector_repo"
    elif proc.returncode == 0:
        print("note: trap fixture unexpectedly collects — shadow may be environment-specific")
