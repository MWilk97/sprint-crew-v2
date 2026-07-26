from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from sprint_crew.orchestrator.workspace_diff import gather_workspace_diff


def test_gather_workspace_diff_truncates(tmp_path: Path) -> None:
    huge = "x" * 30_000
    registry = MagicMock()
    registry.dispatch.return_value = MagicMock(output=huge)
    from sprint_crew import orchestrator

    original = orchestrator.workspace_diff.build_registry
    orchestrator.workspace_diff.build_registry = MagicMock(return_value=registry)
    try:
        result = gather_workspace_diff(tmp_path, max_chars=100)
    finally:
        orchestrator.workspace_diff.build_registry = original
    assert len(result) < len(huge)
    assert "truncated" in result
