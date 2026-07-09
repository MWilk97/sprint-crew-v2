from __future__ import annotations

from sprint_crew.schemas import CodeChange, TaskPlan
from sprint_crew.tools import build_registry


def test_schema_imports() -> None:
    assert TaskPlan.model_fields["ticket_key"] is not None
    assert CodeChange.model_fields["tests_passed"] is not None


def test_tool_registry_registers_all() -> None:
    reg = build_registry()
    assert "read_file" in reg.names()
    assert "run_command" in reg.names()
    assert "apply_patch" in reg.names()
