from __future__ import annotations

import re
import subprocess
from pathlib import Path

from sprint_crew.tools import READONLY_TOOLS, build_registry

_DEFAULT_MAX_CHARS = 24_000
_DIFF_PATH_RE = re.compile(r"^diff --git a/(.+?) b/", re.MULTILINE)


def _normalize_path(path: str) -> str:
    return path.replace("\\", "/").lstrip("./")


def _run_git(args: list[str], workspace_root: Path) -> str:
    proc = subprocess.run(
        ["git", *args],
        cwd=workspace_root,
        capture_output=True,
        text=True,
        check=False,
    )
    return proc.stdout or ""


def _extract_file_hunks(diff_text: str, priority_paths: set[str]) -> str:
    if not priority_paths or not diff_text.strip():
        return diff_text

    normalized_priority = {_normalize_path(p) for p in priority_paths}
    hunks: list[str] = []
    current_file: str | None = None
    current_lines: list[str] = []

    def flush() -> None:
        nonlocal current_file, current_lines
        if current_file and current_lines:
            if _normalize_path(current_file) in normalized_priority:
                hunks.append("\n".join(current_lines))
        current_file = None
        current_lines = []

    for line in diff_text.splitlines():
        match = _DIFF_PATH_RE.match(line)
        if match:
            flush()
            current_file = match.group(1)
            current_lines = [line]
        elif current_file is not None:
            current_lines.append(line)
    flush()
    return "\n".join(hunks) if hunks else diff_text


def gather_workspace_diff(
    workspace_root: Path,
    *,
    max_chars: int = _DEFAULT_MAX_CHARS,
    priority_paths: list[str] | None = None,
) -> str:
    """Return git diff for the workspace, optionally prioritizing plan files."""
    root = workspace_root.resolve()
    registry = build_registry(READONLY_TOOLS)
    result = registry.dispatch("git_diff", {}, workspace_root=root)
    diff = result.output

    if priority_paths:
        stat = _run_git(["diff", "--stat"], root).strip()
        priority_hunks = _extract_file_hunks(diff, set(priority_paths))
        parts: list[str] = []
        if stat:
            parts.append(f"diff --stat\n{stat}")
        if priority_hunks.strip():
            parts.append(priority_hunks)
        if parts:
            diff = "\n\n".join(parts)

    if len(diff) > max_chars:
        return diff[:max_chars] + f"\n... [truncated, {len(diff) - max_chars} chars omitted]"
    return diff
