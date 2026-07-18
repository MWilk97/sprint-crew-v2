from __future__ import annotations

from pathlib import Path

import pytest

from sprint_crew.tools._safety import UnsafePathError, resolve_safe_path


def test_resolve_safe_path_accepts_nested_relative(tmp_path: Path) -> None:
    resolved = resolve_safe_path("src/pkg/foo.py", root=tmp_path)
    assert resolved == (tmp_path / "src/pkg/foo.py").resolve(strict=False)


def test_resolve_safe_path_rejects_git(tmp_path: Path) -> None:
    with pytest.raises(UnsafePathError):
        resolve_safe_path(".git/config", root=tmp_path)


def test_resolve_safe_path_rejects_parent_traversal(tmp_path: Path) -> None:
    with pytest.raises(UnsafePathError):
        resolve_safe_path("../outside.txt", root=tmp_path)


def test_resolve_safe_path_rejects_venv(tmp_path: Path) -> None:
    with pytest.raises(UnsafePathError):
        resolve_safe_path(".venv/lib/python/site.py", root=tmp_path)


def test_resolve_safe_path_rejects_node_modules(tmp_path: Path) -> None:
    with pytest.raises(UnsafePathError):
        resolve_safe_path("node_modules/pkg/index.js", root=tmp_path)


def test_resolve_safe_path_rejects_placeholder_angle_brackets(tmp_path: Path) -> None:
    with pytest.raises(UnsafePathError, match="placeholder"):
        resolve_safe_path("src/<name>.py", root=tmp_path)


def test_resolve_safe_path_rejects_absolute(tmp_path: Path) -> None:
    with pytest.raises(UnsafePathError):
        resolve_safe_path("/etc/passwd", root=tmp_path)
