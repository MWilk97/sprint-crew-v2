from __future__ import annotations

from pathlib import Path

from sprint_crew.tools import build_registry
from sprint_crew.tools.pydantic_ai import build_coder_toolset, workspace_deps


def test_workspace_deps_registry(tmp_path: Path) -> None:
    deps = workspace_deps(tmp_path, mutate=True)
    assert deps.root == tmp_path.resolve()
    assert deps.registry.get("read_file") is not None


def test_coder_toolset_builds() -> None:
    ts = build_coder_toolset()
    assert ts is not None


def test_read_file_via_registry(tmp_path: Path) -> None:
    target = tmp_path / "hello.txt"
    target.write_text("hi", encoding="utf-8")
    reg = build_registry()
    result = reg.dispatch("read_file", {"path": "hello.txt"}, workspace_root=tmp_path)
    assert result.ok
    assert "hi" in result.output
