from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from sprint_crew.tools.apply_patch import ApplyPatchArgs, ApplyPatchTool


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
