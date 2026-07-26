from __future__ import annotations

from pathlib import Path

from sprint_crew.orchestrator.repo_manifest import build_repo_manifest, format_repo_manifest


def test_build_repo_manifest_includes_python_summary(tmp_path: Path) -> None:
    src = tmp_path / "src"
    src.mkdir()
    (src / "ferry.py").write_text('"""Outbound dispatch layer."""\n', encoding="utf-8")

    entries = build_repo_manifest(tmp_path)
    paths = [entry.path for entry in entries]
    assert "src/ferry.py" in paths
    ferry = next(entry for entry in entries if entry.path == "src/ferry.py")
    assert "dispatch" in ferry.summary.lower()

    formatted = format_repo_manifest(entries)
    assert "src/ferry.py" in formatted
    assert "code" in formatted
