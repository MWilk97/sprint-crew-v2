from __future__ import annotations

from pathlib import Path

from sprint_crew.orchestrator.acceptance_tests import run_acceptance_tests


def run_cycle_acceptance_tests(
    workspace_root: Path,
    commands: list[str],
) -> tuple[str, bool]:
    """Single orchestrator entry point for per-cycle acceptance test execution."""
    return run_acceptance_tests(workspace_root, commands)
