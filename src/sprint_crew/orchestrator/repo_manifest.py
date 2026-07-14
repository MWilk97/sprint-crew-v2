from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from sprint_crew.paths import INDEXABLE_SUFFIXES, chunk_kind_for_path, should_skip_path

FileKind = Literal["code", "test", "doc", "config"]


@dataclass(frozen=True)
class FileEntry:
    path: str
    kind: FileKind
    summary: str


def _module_summary(content: str) -> str:
    try:
        tree = ast.parse(content)
    except SyntaxError:
        return ""
    doc = ast.get_docstring(tree)
    if doc and doc.strip():
        first = doc.strip().splitlines()[0].strip()
        return first[:120]
    for line in content.splitlines():
        stripped = line.strip()
        if stripped.startswith("#") and len(stripped) > 2:
            return stripped.lstrip("# ").strip()[:120]
    return ""


def _summary_for_file(path: Path, content: str) -> str:
    if path.suffix.lower() == ".py":
        summary = _module_summary(content)
        if summary:
            return summary
    first_line = next((ln.strip() for ln in content.splitlines() if ln.strip()), "")
    return first_line[:120] if first_line else ""


def build_repo_manifest(workspace: Path, *, max_files: int = 300) -> list[FileEntry]:
    """Deterministic walk of indexable files with one-line summaries."""
    root = workspace.resolve()
    entries: list[FileEntry] = []
    for file_path in sorted(root.rglob("*")):
        if len(entries) >= max_files:
            break
        if not file_path.is_file():
            continue
        try:
            rel = file_path.relative_to(root).as_posix()
        except ValueError:
            continue
        rel_path = Path(rel)
        if should_skip_path(rel_path):
            continue
        if rel_path.suffix.lower() not in INDEXABLE_SUFFIXES:
            continue
        try:
            content = file_path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if not content.strip():
            continue
        chunk_kind = chunk_kind_for_path(rel)
        kind: FileKind
        if chunk_kind == "test":
            kind = "test"
        elif chunk_kind == "doc":
            kind = "doc"
        elif chunk_kind == "config":
            kind = "config"
        else:
            kind = "code"
        entries.append(
            FileEntry(
                path=rel,
                kind=kind,
                summary=_summary_for_file(rel_path, content),
            )
        )
    return entries


def format_repo_manifest(entries: list[FileEntry], *, max_lines: int = 200) -> str:
    if not entries:
        return "(no indexable files)"
    lines: list[str] = []
    for entry in entries[:max_lines]:
        summary = entry.summary or "(no summary)"
        lines.append(f"{entry.path} | {entry.kind} | {summary}")
    if len(entries) > max_lines:
        lines.append(f"... ({len(entries) - max_lines} more files omitted)")
    return "\n".join(lines)
