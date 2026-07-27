"""The structured diff surface: a file list, and one file's hunks (roadmap M6).

Two levels because a change of any size is megabytes of hunks and a UI renders a file tree
before it renders a single file. ``GET .../diff`` carries stats only; hunks are fetched per
file, on expand.

Which snapshot: a console session spawns one sprint session per backlog story, and each is
reviewed up to ``MAX_REVIEW_RETRIES`` times, so ``available`` lists every capture and the
bare route serves the newest. ``sprint_session_id`` + ``attempt`` select any other.
"""

from __future__ import annotations

from fastapi import HTTPException

from sprint_crew.api.console.state import (
    diff_file,
    diff_refs,
    diff_snapshot,
    latest_diff,
    latest_diff_key,
    require_session,
    router,
)
from sprint_crew.schemas.diff import ConsoleDiffPage, FileDiff, WorkspaceDiffSnapshot


async def _resolve_snapshot(
    id: str, sprint_session_id: str | None, attempt: int | None
) -> WorkspaceDiffSnapshot | None:
    """Explicitly addressed snapshot, or the newest one. Both may legitimately be absent."""
    if sprint_session_id is None:
        return await latest_diff(id)
    return await diff_snapshot(id, sprint_session_id, attempt or 0)


async def _resolve_key(
    id: str, sprint_session_id: str | None, attempt: int | None
) -> tuple[str, int] | None:
    """Which snapshot to read one file from, without loading the snapshot's file list.

    Every read filters on the console session id, so an explicitly supplied
    ``sprint_session_id`` needs no lookup to be safe — it cannot address another session's
    snapshot either way.
    """
    if sprint_session_id is not None:
        return sprint_session_id, attempt or 0
    return await latest_diff_key(id)


@router.get("/sessions/{id}/diff", response_model=ConsoleDiffPage)
async def get_console_diff(
    id: str, sprint_session_id: str | None = None, attempt: int | None = None
) -> ConsoleDiffPage:
    """The file list for one snapshot, plus every snapshot this session has.

    A null ``snapshot`` is a 200, not a 404: no diff exists until the first review pass, which
    is most of a run, and a client polling this should not have to read 404 as "normal". 404
    means the *session* is unknown, one meaning per code.
    """
    await require_session(id)
    return ConsoleDiffPage(
        snapshot=await _resolve_snapshot(id, sprint_session_id, attempt),
        available=await diff_refs(id),
    )


@router.get("/sessions/{id}/diff/{path:path}", response_model=FileDiff)
async def get_console_file_diff(
    id: str, path: str, sprint_session_id: str | None = None, attempt: int | None = None
) -> FileDiff:
    """Full hunks for one file. Defaults to the same snapshot the bare route serves."""
    await require_session(id)
    key = await _resolve_key(id, sprint_session_id, attempt)
    if key is None:
        raise HTTPException(status_code=404, detail="no diff snapshot for this session")
    found = await diff_file(id, *key, path)
    if found is None:
        raise HTTPException(status_code=404, detail=f"{path} is not in this diff snapshot")
    return found
