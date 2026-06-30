from __future__ import annotations

import pytest
from pathlib import Path

from sprint_crew.schemas import CodeChange, TaskPlan
from sprint_crew.tools import build_registry
from sprint_crew.tools._safety import UnsafePathError, resolve_safe_path


def test_schema_imports() -> None:
    assert TaskPlan.model_fields["ticket_key"] is not None
    assert CodeChange.model_fields["tests_passed"] is not None


def test_tool_registry_registers_all() -> None:
    reg = build_registry()
    assert "read_file" in reg.names()
    assert "run_command" in reg.names()


def test_resolve_safe_path_rejects_git(tmp_path: Path) -> None:
    with pytest.raises(UnsafePathError):
        resolve_safe_path(".git/config", root=tmp_path)
