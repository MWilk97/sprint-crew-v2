"""Parser, renderer, and capture tests (roadmap M6).

Fixtures are real ``git diff`` output produced by a real repository rather than hand-written
strings: the parser's whole job is to be faithful to what git emits, and a hand-written
fixture only proves it is faithful to what the test author remembered.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from sprint_crew.config import get_settings
from sprint_crew.orchestrator.diff_capture import capture_snapshot
from sprint_crew.orchestrator.diff_parse import (
    parse_unified_diff,
    render_unified_diff,
)


def _git(workspace: Path, *argv: str) -> str:
    proc = subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", *argv],
        cwd=workspace,
        capture_output=True,
        text=True,
        check=False,
    )
    return proc.stdout


@pytest.fixture
def dirty_repo(tmp_path: Path) -> Path:
    """A workspace holding one file of each action, plus a binary and a no-newline file."""
    _git(tmp_path, "init", "-q", ".")
    (tmp_path / "keep.py").write_text("a\nb\nc\n", encoding="utf-8")
    (tmp_path / "gone.py").write_text("x\ny\n", encoding="utf-8")
    (tmp_path / "one.py").write_text("only\n", encoding="utf-8")
    (tmp_path / "blob.bin").write_bytes(b"\x00\x01original")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-qm", "init")

    (tmp_path / "keep.py").write_text("a\nB\nc\nd\n", encoding="utf-8")
    (tmp_path / "gone.py").unlink()
    (tmp_path / "one.py").write_text("ONE\n", encoding="utf-8")
    (tmp_path / "blob.bin").write_bytes(b"\x00\x01changed\x02")
    (tmp_path / "fresh.py").write_text("new1\nnew2", encoding="utf-8")
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "mod.py").write_text("inner\n", encoding="utf-8")
    return tmp_path


def test_round_trip_is_byte_for_byte(dirty_repo: Path) -> None:
    """The only cheap proof the parser drops nothing. Covers context/add/del lines, a
    deletion, a binary file, and the ``@@ -1 +1 @@`` form where git elides a count of 1.
    """
    raw = _git(dirty_repo, "diff")

    assert render_unified_diff(parse_unified_diff(raw)) == raw


def test_round_trip_keeps_the_no_newline_marker(dirty_repo: Path) -> None:
    """``\\ No newline at end of file`` is a line in the byte stream, so it has to survive
    as one; treating it as a property of the line before it loses its position."""
    raw = _git(dirty_repo, "diff", "--no-index", "--", "/dev/null", "fresh.py")
    parsed = parse_unified_diff(raw)

    assert [line.kind for line in parsed[0].hunks[0].lines] == ["add", "add", "no_newline"]
    assert render_unified_diff(parsed) == raw


def test_line_numbers_track_both_sides(dirty_repo: Path) -> None:
    (file_diff,) = parse_unified_diff(_git(dirty_repo, "diff", "--", "keep.py"))
    lines = file_diff.hunks[0].lines

    # a(1,1) -b(2,-) +B(-,2) c(3,3) +d(-,4)
    assert [(line.kind, line.old_lineno, line.new_lineno) for line in lines] == [
        ("context", 1, 1),
        ("del", 2, None),
        ("add", None, 2),
        ("context", 3, 3),
        ("add", None, 4),
    ]


def test_capture_reports_every_action_with_line_counts(dirty_repo: Path) -> None:
    """The roadmap's done-when: one created, one modified, one deleted file, each with the
    right action and counts. ``action`` comes from porcelain, which is why the created file
    is not reported as "modified" the way ``formatter._derive_files_changed`` does.
    """
    snapshot, files = capture_snapshot(dirty_repo, sprint_session_id="s1", ticket_key="DEMO-1")
    by_path = {f.path: f for f in snapshot.files}

    assert by_path["keep.py"].action == "modified"
    assert (by_path["keep.py"].additions, by_path["keep.py"].deletions) == (2, 1)
    assert by_path["gone.py"].action == "deleted"
    assert (by_path["gone.py"].additions, by_path["gone.py"].deletions) == (0, 2)
    assert by_path["fresh.py"].action == "created"
    assert by_path["fresh.py"].additions == 2
    assert snapshot.sprint_session_id == "s1"
    assert snapshot.git_sha
    assert [f.path for f in files] == sorted(f.path for f in files)


def test_capture_sees_untracked_files_git_diff_omits(dirty_repo: Path) -> None:
    """Plain ``git diff`` shows neither ``fresh.py`` nor a file inside a wholly new
    directory, so a new file — the most interesting thing an agent produces — was
    previously invisible in the review surface.
    """
    assert "fresh.py" not in _git(dirty_repo, "diff")

    _, files = capture_snapshot(dirty_repo, sprint_session_id="s1")
    created = {f.path for f in files if f.action == "created"}

    assert {"fresh.py", "pkg/mod.py"} <= created


def test_capture_marks_binary_files_without_hunks(dirty_repo: Path) -> None:
    _, files = capture_snapshot(dirty_repo, sprint_session_id="s1")
    (blob,) = [f for f in files if f.path == "blob.bin"]

    assert blob.binary is True
    assert blob.hunks == []


def test_capture_truncates_an_oversized_file_but_keeps_it(
    dirty_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A file over the cap is shortened and flagged, never dropped — a silently absent file
    is worse than a visibly partial one. The hunk counts are rescoped to the lines that
    survived so the payload does not describe content it no longer carries.
    """
    monkeypatch.setenv("DIFF_MAX_FILE_BYTES", "2000")
    get_settings.cache_clear()
    (dirty_repo / "big.py").write_text("\n".join(f"line {n}" for n in range(4000)), "utf-8")

    _, files = capture_snapshot(dirty_repo, sprint_session_id="s1")
    (big,) = [f for f in files if f.path == "big.py"]

    assert big.truncated is True
    assert big.additions < 4000
    assert big.additions == sum(1 for h in big.hunks for line in h.lines if line.kind == "add")
    assert all(
        h.new_lines == sum(1 for line in h.lines if line.kind in ("context", "add"))
        for h in big.hunks
    )


def test_capture_skips_noise_paths(dirty_repo: Path) -> None:
    (dirty_repo / "__pycache__").mkdir()
    (dirty_repo / "__pycache__" / "x.pyc").write_text("junk", encoding="utf-8")

    _, files = capture_snapshot(dirty_repo, sprint_session_id="s1")

    assert not [f for f in files if "__pycache__" in f.path]


def test_dotfile_paths_keep_their_leading_dot(dirty_repo: Path) -> None:
    """``paths.normalize_path`` does ``lstrip("./")``, which turns ``.gitignore`` into
    ``gitignore``; a diff surface that renamed dotfiles would make them unfetchable by
    their real name.
    """
    (dirty_repo / ".gitignore").write_text("*.log\n", encoding="utf-8")

    _, files = capture_snapshot(dirty_repo, sprint_session_id="s1")

    assert ".gitignore" in {f.path for f in files}
