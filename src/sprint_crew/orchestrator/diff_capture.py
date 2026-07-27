"""Capture a structured diff snapshot of a workspace (roadmap M6).

Two things plain ``git diff`` cannot tell you, both of which the review UI needs:

- **Untracked files are invisible to it.** A newly created file — the most interesting kind
  of change an agent makes — produces no diff at all. Each one is diffed separately with
  ``--no-index`` against ``/dev/null``, which yields a real unified diff the same parser
  eats. Deliberately not ``git add -N``: that mutates the index, and ship later runs
  ``git add -A`` over the same repo.
- **It does not report the action.** ``git status --porcelain`` does, and it can see the
  index, so it is the authority for created/modified/deleted where the two disagree.

Renames are left as delete + create. ``-M`` is not passed, so git already reports them that
way, and modelling similarity is not worth it for a v1 review surface.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from sprint_crew.config import get_settings
from sprint_crew.git_exec import git_output, git_run
from sprint_crew.orchestrator.diff_parse import (
    action_from_header,
    count_add_del,
    diff_path,
    estimated_size,
    parse_unified_diff,
)
from sprint_crew.orchestrator.diff_store import diff_store
from sprint_crew.orchestrator.emitter import current_emitter
from sprint_crew.orchestrator.plan_coverage import is_noise_path
from sprint_crew.schemas.change import FileAction
from sprint_crew.schemas.diff import DiffHunk, DiffLine, FileDiff, WorkspaceDiffSnapshot
from sprint_crew.schemas.session import agent_event

logger = logging.getLogger(__name__)


def capture_snapshot(
    workspace_root: Path,
    *,
    sprint_session_id: str,
    ticket_key: str = "",
    attempt: int = 0,
    console_session_id: str | None = None,
) -> tuple[WorkspaceDiffSnapshot, list[FileDiff]]:
    """Read the working tree and return the snapshot plus one ``FileDiff`` per file."""
    root = workspace_root.resolve()
    settings = get_settings()
    statuses = _porcelain_statuses(root)

    files = [f for f in parse_unified_diff(git_output(["diff"], root)) if not is_noise_path(f.path)]
    seen = {f.path for f in files}
    for path, code in statuses.items():
        if code != "??" or path in seen or is_noise_path(path):
            continue
        files.extend(_diff_untracked(root, path))

    for file_diff in files:
        code = statuses.get(file_diff.path)
        if code is not None:
            file_diff.action = _action_from_porcelain(code, file_diff.header_lines)

    files.sort(key=lambda f: f.path)
    files, dropped = _apply_caps(files, settings.diff_max_files, settings.diff_max_file_bytes)

    snapshot = WorkspaceDiffSnapshot(
        console_session_id=console_session_id,
        sprint_session_id=sprint_session_id,
        ticket_key=ticket_key,
        attempt=attempt,
        git_sha=_head_sha(root),
        files=[f.summary() for f in files],
        total_additions=sum(f.additions for f in files),
        total_deletions=sum(f.deletions for f in files),
        truncated=dropped,
    )
    return snapshot, files


async def record_diff_snapshot(
    workspace_root: Path,
    *,
    sprint_session_id: str,
    ticket_key: str = "",
    attempt: int = 0,
) -> WorkspaceDiffSnapshot | None:
    """Capture, persist, and announce a snapshot — a no-op outside a console run.

    Gated on the emitter rather than on a flag: the console review surface is the only
    consumer, so a direct ``/sprint/*`` run, ``smoke_cycle``, and most unit tests should not
    pay for several git subprocesses and a parse per review pass.

    Never raises. A snapshot is a read-only convenience on top of a cycle that is already
    doing real work; failing the cycle because a diff could not be parsed would be a poor
    trade, and the reviewer has its own copy of the diff text either way.
    """
    emitter = current_emitter()
    if emitter is None or not get_settings().diff_snapshot_enabled:
        return None
    try:
        snapshot, files = await asyncio.to_thread(
            capture_snapshot,
            workspace_root,
            sprint_session_id=sprint_session_id,
            ticket_key=ticket_key,
            attempt=attempt,
            console_session_id=emitter.console_session_id,
        )
        await asyncio.to_thread(diff_store().save, snapshot, files)
    except Exception:
        logger.warning("diff snapshot failed for session %s", sprint_session_id, exc_info=True)
        return None

    # Emitted on the loop thread, not inside the to_thread above: EventBus.publish requires
    # it (see orchestrator/emitter.py).
    emitter.emit(
        agent_event(
            "orchestrator",
            "diff_updated",
            f"{len(snapshot.files)} file(s) changed "
            f"(+{snapshot.total_additions}/-{snapshot.total_deletions})",
            sprint_session_id=sprint_session_id,
            attempt=attempt,
            files_changed=len(snapshot.files),
            additions=snapshot.total_additions,
            deletions=snapshot.total_deletions,
            truncated=snapshot.truncated,
        )
    )
    return snapshot


def _porcelain_statuses(root: Path) -> dict[str, str]:
    """``path -> XY`` status code. ``-uall`` so a wholly new directory is listed file by
    file rather than as one ``?? dir/`` entry that cannot be diffed."""
    statuses: dict[str, str] = {}
    for line in git_output(["status", "--porcelain", "-uall"], root).splitlines():
        if len(line) < 4:
            continue
        code = line[:2]
        entry = line[3:].strip()
        if " -> " in entry:
            entry = entry.split(" -> ", 1)[1]
        if entry.startswith('"') and entry.endswith('"'):
            entry = entry[1:-1]
        if entry:
            statuses[diff_path(entry)] = code
    return statuses


def _diff_untracked(root: Path, path: str) -> list[FileDiff]:
    # --no-index exits non-zero when the files differ, which is the normal case here, so
    # the exit code is ignored exactly as git_output does for every other read-only query.
    text = git_output(["diff", "--no-index", "--", "/dev/null", path], root)
    parsed = parse_unified_diff(text)
    for file_diff in parsed:
        # The a-side is /dev/null; the real name is the one we asked about.
        file_diff.path = path
        file_diff.old_path = None
        file_diff.action = "created"
    return parsed


def _action_from_porcelain(code: str, header_lines: list[str]) -> FileAction:
    if code == "??" or "A" in code or "R" in code:
        return "created"
    if "D" in code:
        return "deleted"
    if "M" in code:
        return "modified"
    return action_from_header(header_lines)


def _apply_caps(
    files: list[FileDiff], max_files: int, max_file_bytes: int
) -> tuple[list[FileDiff], bool]:
    """Shorten oversized files in place; drop files past the count cap.

    A shortened file keeps its own ``truncated`` flag, so the UI can say which file it is
    not showing all of. The snapshot-level flag means whole files are missing, which is a
    different and worse thing to hide.
    """
    for file_diff in files:
        _truncate_file(file_diff, max_file_bytes)
    if len(files) <= max_files:
        return files, False
    return files[:max_files], True


def _truncate_file(file_diff: FileDiff, max_bytes: int) -> None:
    """Keep the leading hunks that fit in the budget, splitting the one that straddles it.

    A truncated file no longer renders back to the bytes git produced — that is what
    ``truncated`` says. Its hunk counts are recomputed from the lines that survived so the
    payload stays internally consistent rather than describing lines it does not carry.
    """
    if estimated_size(file_diff) <= max_bytes:
        return

    used = sum(len(line) + 1 for line in file_diff.header_lines)
    kept: list[DiffHunk] = []
    for hunk in file_diff.hunks:
        overhead = 32 + len(hunk.section)
        cost = overhead + sum(len(line.content) + 2 for line in hunk.lines)
        if used + cost <= max_bytes:
            kept.append(hunk)
            used += cost
            continue
        room = max_bytes - used - overhead
        lines: list[DiffLine] = []
        for line in hunk.lines:
            room -= len(line.content) + 2
            if room < 0:
                break
            lines.append(line)
        if lines:
            kept.append(_rescoped_hunk(hunk, lines))
        break

    file_diff.hunks = kept
    file_diff.truncated = True
    file_diff.additions, file_diff.deletions = count_add_del(kept)


def _rescoped_hunk(hunk: DiffHunk, lines: list[DiffLine]) -> DiffHunk:
    return hunk.model_copy(
        update={
            "lines": lines,
            "old_lines": sum(1 for line in lines if line.kind in ("context", "del")),
            "new_lines": sum(1 for line in lines if line.kind in ("context", "add")),
        }
    )


def _head_sha(root: Path) -> str:
    proc = git_run(["rev-parse", "HEAD"], cwd=root)
    return proc.stdout.strip() if proc.returncode == 0 else ""
