from __future__ import annotations

import shutil
from pathlib import Path
from unittest.mock import patch

import pytest

from sprint_crew.tools._safety import UnsafePathError, resolve_safe_path
from sprint_crew.tools.apply_patch import ApplyPatchArgs, ApplyPatchTool
from sprint_crew.tools.grep import GrepArgs, GrepTool
from sprint_crew.tools.list_directory import ListDirectoryArgs, ListDirectoryTool
from sprint_crew.tools.read_file import ReadFileArgs, ReadFileTool
from sprint_crew.tools.write_file import WriteFileArgs, WriteFileTool


@pytest.fixture
def patch_available() -> None:
    if shutil.which("patch") is None:
        pytest.skip("patch(1) not installed")


def test_apply_patch_applies_unified_diff(tmp_path: Path, patch_available: None) -> None:
    target = tmp_path / "greeter.py"
    target.write_text("def hello():\n    return 'hi'\n", encoding="utf-8")
    diff = """\
--- a/greeter.py
+++ b/greeter.py
@@ -1,2 +1,2 @@
 def hello():
-    return 'hi'
+    return 'hello'
"""
    tool = ApplyPatchTool()
    result = tool.execute(ApplyPatchArgs(patch=diff), workspace_root=tmp_path)
    assert result.ok is True
    assert target.read_text(encoding="utf-8") == "def hello():\n    return 'hello'\n"


def test_apply_patch_fails_on_conflict(tmp_path: Path, patch_available: None) -> None:
    target = tmp_path / "greeter.py"
    target.write_text("def hello():\n    return 'wrong'\n", encoding="utf-8")
    diff = """\
--- a/greeter.py
+++ b/greeter.py
@@ -1,2 +1,2 @@
 def hello():
-    return 'hi'
+    return 'hello'
"""
    tool = ApplyPatchTool()
    result = tool.execute(ApplyPatchArgs(patch=diff), workspace_root=tmp_path)
    assert result.ok is False


def test_apply_patch_rejects_unsafe_path(tmp_path: Path) -> None:
    diff = """\
--- a/.git/config
+++ b/.git/config
@@ -1,1 +1,1 @@
 x
+y
"""
    tool = ApplyPatchTool()
    result = tool.execute(ApplyPatchArgs(patch=diff), workspace_root=tmp_path)
    assert result.ok is False
    assert "forbidden" in result.output.lower() or "escapes" in result.output.lower()


def test_apply_patch_missing_binary(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def _raise_not_found(*_args, **_kwargs):
        raise FileNotFoundError

    monkeypatch.setattr("sprint_crew.tools.apply_patch.subprocess.run", _raise_not_found)
    tool = ApplyPatchTool()
    result = tool.execute(ApplyPatchArgs(patch="--- a\n+++ b\n"), workspace_root=tmp_path)
    assert result.ok is False
    assert "patch" in result.output.lower()


def test_write_file_rejects_oversized_content(tmp_path: Path) -> None:
    tool = WriteFileTool()
    huge = "x" * 70000
    with patch("sprint_crew.tools.write_file.get_settings") as settings_mock:
        settings_mock.return_value.max_write_file_bytes = 65536
        result = tool.execute(WriteFileArgs(path="big.py", content=huge), workspace_root=tmp_path)

    assert result.ok is False
    assert "apply_patch" in result.output


def test_write_file_allows_small_content(tmp_path: Path) -> None:
    tool = WriteFileTool()
    with patch("sprint_crew.tools.write_file.get_settings") as settings_mock:
        settings_mock.return_value.max_write_file_bytes = 65536
        result = tool.execute(
            WriteFileArgs(path="small.py", content="ok\n"), workspace_root=tmp_path
        )

    assert result.ok is True
    assert (tmp_path / "small.py").read_text(encoding="utf-8") == "ok\n"


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
