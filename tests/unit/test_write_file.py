from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from sprint_crew.tools.write_file import WriteFileArgs, WriteFileTool


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
