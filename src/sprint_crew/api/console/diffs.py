"""The structured diff surface: a file list, one file's hunks, and the review (M6, M7).

Two levels because a change of any size is megabytes of hunks and a UI renders a file tree
before it renders a single file. ``GET .../diff`` carries stats only; hunks are fetched per
file, on expand.

Which snapshot: a console session spawns one sprint session per backlog story, and each is
reviewed up to ``MAX_REVIEW_RETRIES`` times, so ``available`` lists every capture and the
bare route serves the newest. ``sprint_session_id`` + ``attempt`` select any other.

``POST .../diff/decisions`` is the only write in this module, and the only endpoint in the
API that unblocks a running graph (ADR 0015).
"""

from __future__ import annotations

import asyncio

from fastapi import HTTPException

from sprint_crew.api.console.state import (
    _lock_for,
    _utc_now_iso,
    close_review,
    diff_file,
    diff_refs,
    diff_snapshot,
    emit,
    latest_diff,
    latest_diff_key,
    pending_review,
    record_review_decisions,
    require_session,
    review_state,
    router,
)
from sprint_crew.orchestrator.review_gate import review_gate
from sprint_crew.schemas.diff import (
    ConsoleDiffPage,
    DiffDecisionsRequest,
    DiffReviewState,
    FileDiff,
    WorkspaceDiffSnapshot,
)
from sprint_crew.schemas.session import agent_event


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
    # Three independent reads, each its own connection: awaiting them in turn paid three
    # round-trips of latency for no ordering the payload needs.
    snapshot, available, review = await asyncio.gather(
        _resolve_snapshot(id, sprint_session_id, attempt),
        diff_refs(id),
        pending_review(id),
    )
    return ConsoleDiffPage(snapshot=snapshot, available=available, review=review)


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


@router.post("/sessions/{id}/diff/decisions", response_model=DiffReviewState)
async def submit_diff_decisions(id: str, body: DiffDecisionsRequest) -> DiffReviewState:
    """Record per-file verdicts and, when ``submit`` is set, release the parked run.

    Decisions accumulate across calls so a half-finished review survives a reload; only
    ``submit`` closes the review, and it accepts whatever is still undecided. 409 unless a
    review is actually open — a verdict nobody is waiting for would silently do nothing,
    and a client whose view is that stale needs to hear about it.
    """
    await require_session(id)
    async with _lock_for(id):
        session = await require_session(id)
        review = await pending_review(id)
        if review is None:
            raise HTTPException(status_code=409, detail="no diff review is open for this session")
        if body.sprint_session_id is not None and (
            body.sprint_session_id != review.sprint_session_id
            or (body.attempt or 0) != review.attempt
        ):
            raise HTTPException(
                status_code=409,
                detail=(
                    f"the open review is {review.sprint_session_id} "
                    f"attempt {review.attempt}, not the snapshot addressed"
                ),
            )

        key = (review.sprint_session_id, review.attempt)
        # The review already carries the snapshot's whole path universe — decided plus
        # undecided — so validating against it saves re-reading the file rows.
        known = {d.path for d in review.decisions} | set(review.undecided_paths)
        if unknown := sorted({d.path for d in body.decisions} - known):
            raise HTTPException(
                status_code=400, detail=f"not in this diff snapshot: {', '.join(unknown)}"
            )

        now = _utc_now_iso()
        if body.decisions:
            # Server-stamped: a client clock is not a fact about when the review happened.
            await record_review_decisions(
                id, *key, [d.model_copy(update={"decided_at": now}) for d in body.decisions]
            )
        if body.submit:
            await close_review(id, *key, status="decided", decided_at=now)

        updated = await review_state(id, *key) or review
        rejected = [d for d in updated.decisions if d.decision == "reject"]
        await emit(
            session,
            agent_event(
                "user",
                "review_decisions_recorded",
                f"{len(updated.decisions)} file(s) decided, {len(rejected)} rejected"
                + (" — submitted" if body.submit else ""),
                # A UI that autosaves per click would otherwise put one info-level event on
                # the timeline per file; only the submit is a milestone worth showing.
                level="info" if body.submit else "debug",
                sprint_session_id=review.sprint_session_id,
                attempt=review.attempt,
                submitted=body.submit,
                rejected=[d.path for d in rejected],
                undecided=len(updated.undecided_paths),
            ),
        )
        if body.submit:
            # Store first, notify second: the table is committed above, so a notification
            # lost to a restart or a second worker costs the run its wait, never its answer.
            review_gate().notify(*key)
        return updated
