from __future__ import annotations

from pathlib import Path

import pytest

from sprint_crew.tools._safety import UnsafePathError, resolve_safe_path
from sprint_crew.tools.grep import GrepArgs, GrepTool
from sprint_crew.tools.list_directory import ListDirectoryArgs, ListDirectoryTool
from sprint_crew.tools.read_file import ReadFileArgs, ReadFileTool


def test_read_file_returns_content(tmp_path: Path) -> None:
    target = tmp_path / "hello.txt"
    target.write_text("hi there\n", encoding="utf-8")
    tool = ReadFileTool()
    result = tool.execute(ReadFileArgs(path="hello.txt"), workspace_root=tmp_path)
    assert result.ok is True
    assert "hi there" in result.output


def test_read_file_rejects_unsafe_path(tmp_path: Path) -> None:
    tool = ReadFileTool()
    with pytest.raises(UnsafePathError):
        tool.execute(ReadFileArgs(path=".git/config"), workspace_root=tmp_path)


def test_grep_finds_pattern_in_workspace(tmp_path: Path) -> None:
    src = tmp_path / "src"
    src.mkdir()
    (src / "greeter.py").write_text("def hello():\n    return 'hello'\n", encoding="utf-8")
    tool = GrepTool()
    result = tool.execute(GrepArgs(pattern=r"def hello", path="src"), workspace_root=tmp_path)
    assert result.ok is True
    assert "greeter.py" in result.output


def test_list_directory_lists_entries(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("a\n", encoding="utf-8")
    (tmp_path / "pkg").mkdir()
    tool = ListDirectoryTool()
    result = tool.execute(ListDirectoryArgs(path="."), workspace_root=tmp_path)
    assert result.ok is True
    assert "a.py" in result.output
    assert "pkg" in result.output


def test_resolve_safe_path_rejects_absolute(tmp_path: Path) -> None:
    with pytest.raises(UnsafePathError):
        resolve_safe_path("/etc/passwd", root=tmp_path)
