from __future__ import annotations

from pathlib import Path

from sprint_crew.vector.chunker import chunk_file, count_indexable_files, iter_workspace_chunks


def test_python_chunk_by_function(tmp_path: Path) -> None:
    src = tmp_path / "greeter.py"
    src.write_text(
        "def hello():\n    return 'hello'\n\n\ndef goodbye():\n    return 'bye'\n",
        encoding="utf-8",
    )
    chunks = chunk_file("greeter.py", src.read_text(encoding="utf-8"))
    assert len(chunks) >= 2
    assert all(c.path == "greeter.py" for c in chunks)
    assert any("hello" in c.text for c in chunks)


def test_skips_forbidden_paths(tmp_path: Path) -> None:
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "config").write_text("secret\n", encoding="utf-8")
    (tmp_path / "main.py").write_text("x = 1\n", encoding="utf-8")
    assert count_indexable_files(tmp_path) == 1
    chunks = iter_workspace_chunks(tmp_path)
    assert len(chunks) == 1
    assert chunks[0].path == "main.py"


def test_chunk_display_includes_path_header(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("def f():\n    pass\n", encoding="utf-8")
    chunks = iter_workspace_chunks(tmp_path)
    assert chunks
    display = chunks[0].display_text()
    assert "# path: a.py" in display
    assert "# kind:" in display
