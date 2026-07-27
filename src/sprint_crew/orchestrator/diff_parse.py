"""Unified-diff parser and renderer (roadmap M6).

Nothing in the tree parsed hunks before this: ``workspace_diff._extract_file_hunks`` splits
per *file* despite its name, and ``paths.paths_from_diff`` only reads ``diff --git`` headers.

The renderer exists to make the parser testable — ``render_unified_diff(parse_unified_diff(x))``
must return ``x`` byte for byte. That round-trip is the only cheap proof that no line kind,
count, or header was silently dropped, and it is what lets the API serve hunks per file
without anyone having to trust that reassembly is faithful.

One normalisation is deliberate: an empty context line is stored as ``content=""`` and rendered
as a single space, because that is what git emits. A hand-written diff using a bare empty line
for the same thing round-trips to git's spelling, not its own.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

from sprint_crew.schemas.change import FileAction
from sprint_crew.schemas.diff import DiffHunk, DiffLine, FileDiff

_FILE_HEADER_RE = re.compile(r"^diff --git a/(.+?) b/(.+)$")
_HUNK_HEADER_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(?: (.*))?$")
_RENAME_FROM_RE = re.compile(r"^rename from (.+)$")

_PREFIX: dict[str, str] = {"context": " ", "add": "+", "del": "-", "no_newline": "\\ "}


def diff_path(raw: str) -> str:
    """Separator-normalise a path from diff or porcelain output, and nothing else.

    Not ``paths.normalize_path``: that one does ``lstrip("./")``, which eats the leading dot
    of every dotfile — a ``.gitignore`` change would be served to the UI as ``gitignore`` and
    then be unfindable by its real name.
    """
    return raw.replace("\\", "/")


def parse_unified_diff(text: str) -> list[FileDiff]:
    """Parse ``git diff`` output into one ``FileDiff`` per file, in the order given."""
    files: list[FileDiff] = []
    lines = text.split("\n")
    # git output ends with a newline, which split leaves as a trailing empty element.
    if lines and lines[-1] == "":
        lines.pop()

    index = 0
    while index < len(lines):
        if not lines[index].startswith("diff --git "):
            index += 1
            continue
        file_diff, index = _parse_file(lines, index)
        if file_diff is not None:
            files.append(file_diff)
    return files


def render_unified_diff(files: Iterable[FileDiff]) -> str:
    """Rebuild the diff text a list of ``FileDiff``s came from."""
    out: list[str] = []
    for file_diff in files:
        out.extend(file_diff.header_lines)
        for hunk in file_diff.hunks:
            out.append(_render_hunk_header(hunk))
            out.extend(_PREFIX[line.kind] + line.content for line in hunk.lines)
    return "\n".join(out) + "\n" if out else ""


def count_add_del(hunks: Iterable[DiffHunk]) -> tuple[int, int]:
    """Additions and deletions implied by a hunk list.

    One helper rather than the same reduction at each site: the counts are stored on
    ``FileDiff``, so anything that rewrites ``hunks`` (truncation, today) has to restate
    them, and a copy that drifts leaves a file whose header disagrees with its own body.
    """
    lines = [line for hunk in hunks for line in hunk.lines]
    return (
        sum(1 for line in lines if line.kind == "add"),
        sum(1 for line in lines if line.kind == "del"),
    )


def estimated_size(file_diff: FileDiff) -> int:
    """Byte cost of one file's diff, without paying for a full render.

    Used by the capture path to apply ``DIFF_MAX_FILE_BYTES``, where an exact count buys
    nothing over an estimate that is within a byte per line.
    """
    total = sum(len(line) + 1 for line in file_diff.header_lines)
    for hunk in file_diff.hunks:
        total += 32 + len(hunk.section)
        total += sum(len(line.content) + 2 for line in hunk.lines)
    return total


def _parse_file(lines: list[str], start: int) -> tuple[FileDiff | None, int]:
    match = _FILE_HEADER_RE.match(lines[start])
    if match is None:
        return None, start + 1

    old_path = diff_path(match.group(1))
    new_path = diff_path(match.group(2))

    header: list[str] = [lines[start]]
    index = start + 1
    while index < len(lines):
        line = lines[index]
        if line.startswith("diff --git ") or line.startswith("@@ "):
            break
        header.append(line)
        index += 1

    hunks: list[DiffHunk] = []
    while index < len(lines) and lines[index].startswith("@@ "):
        hunk, index = _parse_hunk(lines, index)
        if hunk is None:
            break
        hunks.append(hunk)

    additions, deletions = count_add_del(hunks)
    for line in header:
        rename = _RENAME_FROM_RE.match(line)
        if rename is not None:
            old_path = diff_path(rename.group(1))

    return (
        FileDiff(
            path=new_path,
            old_path=old_path if old_path != new_path else None,
            action=action_from_header(header),
            additions=additions,
            deletions=deletions,
            binary=any(line.startswith("Binary files ") for line in header),
            header_lines=header,
            hunks=hunks,
        ),
        index,
    )


def _parse_hunk(lines: list[str], start: int) -> tuple[DiffHunk | None, int]:
    match = _HUNK_HEADER_RE.match(lines[start])
    if match is None:
        return None, start + 1

    old_start = int(match.group(1))
    # git elides ",count" when the count is 1; absent therefore means one line, not zero.
    old_lines = int(match.group(2)) if match.group(2) is not None else 1
    new_start = int(match.group(3))
    new_lines = int(match.group(4)) if match.group(4) is not None else 1
    section = match.group(5) or ""

    body: list[DiffLine] = []
    old_no, new_no = old_start, new_start
    remaining_old, remaining_new = old_lines, new_lines
    index = start + 1
    while index < len(lines) and (remaining_old > 0 or remaining_new > 0):
        line = lines[index]
        if line.startswith("diff --git ") or line.startswith("@@ "):
            break
        if line.startswith("\\"):
            # The no-newline marker belongs to the line before it and consumes no count.
            body.append(DiffLine(kind="no_newline", content=line[2:]))
        elif line.startswith("+"):
            body.append(DiffLine(kind="add", content=line[1:], new_lineno=new_no))
            new_no += 1
            remaining_new -= 1
        elif line.startswith("-"):
            body.append(DiffLine(kind="del", content=line[1:], old_lineno=old_no))
            old_no += 1
            remaining_old -= 1
        elif line.startswith(" ") or line == "":
            body.append(
                DiffLine(kind="context", content=line[1:], old_lineno=old_no, new_lineno=new_no)
            )
            old_no += 1
            new_no += 1
            remaining_old -= 1
            remaining_new -= 1
        else:
            break
        index += 1

    # A trailing no-newline marker sits after the counts are exhausted, so the loop above
    # has already stopped by the time it is reached.
    if index < len(lines) and lines[index].startswith("\\"):
        body.append(DiffLine(kind="no_newline", content=lines[index][2:]))
        index += 1

    return (
        DiffHunk(
            old_start=old_start,
            old_lines=old_lines,
            new_start=new_start,
            new_lines=new_lines,
            section=section,
            lines=body,
        ),
        index,
    )


def action_from_header(header_lines: Iterable[str]) -> FileAction:
    """What the ``diff --git`` preamble claims happened to the file.

    ``git status --porcelain`` is the better authority — it can see the index — so the
    capture path overrides this where the two disagree. This is the fallback for a diff
    that arrives without a status alongside it.
    """
    for line in header_lines:
        if line.startswith("new file mode"):
            return "created"
        if line.startswith("deleted file mode"):
            return "deleted"
    return "modified"


def _render_hunk_header(hunk: DiffHunk) -> str:
    old = _render_range(hunk.old_start, hunk.old_lines)
    new = _render_range(hunk.new_start, hunk.new_lines)
    header = f"@@ -{old} +{new} @@"
    return f"{header} {hunk.section}" if hunk.section else header


def _render_range(start: int, count: int) -> str:
    return str(start) if count == 1 else f"{start},{count}"
