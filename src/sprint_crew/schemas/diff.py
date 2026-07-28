"""Structured per-file, per-hunk diff model for the console review surface (roadmap M6).

Deliberately not the same representation as ``SprintState["workspace_diff"]``. That one is a
truncated string sized for an agent's prompt budget; this one is structure a UI can render a
file tree and a hunk view from. Sharing one shape would compromise both, so the 24 000-char
prompt diff stays exactly as it is.

Lossless by construction: ``FileDiff.header_lines`` keeps the ``diff --git`` preamble verbatim
and ``no_newline`` is a line kind rather than a flag, so ``render_unified_diff`` can rebuild
the bytes git produced. ``orchestrator/diff_parse.py`` pins that round-trip with a test.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, model_validator

from sprint_crew.schemas._base import STRICT
from sprint_crew.schemas.change import FileAction
from sprint_crew.schemas.session import utc_now_iso

#: ``no_newline`` is the ``\ No newline at end of file`` marker. It is a line rather than a
#: property of the line before it because that is where it sits in the byte stream.
DiffLineKind = Literal["context", "add", "del", "no_newline"]


class DiffLine(BaseModel):
    model_config = STRICT

    kind: DiffLineKind
    content: str = ""
    # Null on the side the line does not exist in: an added line has no old number.
    old_lineno: int | None = Field(default=None, ge=1)
    new_lineno: int | None = Field(default=None, ge=1)


class DiffHunk(BaseModel):
    model_config = STRICT

    old_start: int = Field(..., ge=0)
    old_lines: int = Field(..., ge=0)
    new_start: int = Field(..., ge=0)
    new_lines: int = Field(..., ge=0)
    # The heading git appends after the closing ``@@`` (usually the enclosing function).
    section: str = ""
    lines: list[DiffLine] = Field(default_factory=list)


class DiffFileSummary(BaseModel):
    """One file's stats without its hunks — the payload of the file-tree endpoint.

    Split from ``FileDiff`` so rendering a file tree does not mean shipping every hunk of
    every file; a large change is megabytes of hunks and only one file is open at a time.
    """

    model_config = STRICT

    path: str = Field(..., min_length=1)
    # Set when the file was renamed or deleted and the old name differs from ``path``.
    old_path: str | None = None
    action: FileAction
    additions: int = Field(default=0, ge=0)
    deletions: int = Field(default=0, ge=0)
    binary: bool = False
    truncated: bool = False


class FileDiff(BaseModel):
    model_config = STRICT

    path: str = Field(..., min_length=1)
    old_path: str | None = None
    action: FileAction
    additions: int = Field(default=0, ge=0)
    deletions: int = Field(default=0, ge=0)
    # Binary files have no hunks at all — git reports only that they differ.
    binary: bool = False
    truncated: bool = False
    header_lines: list[str] = Field(default_factory=list)
    hunks: list[DiffHunk] = Field(default_factory=list)

    def summary(self) -> DiffFileSummary:
        return DiffFileSummary(
            path=self.path,
            old_path=self.old_path,
            action=self.action,
            additions=self.additions,
            deletions=self.deletions,
            binary=self.binary,
            truncated=self.truncated,
        )


class WorkspaceDiffSnapshot(BaseModel):
    """What one sprint session's working tree looked like at one review pass.

    Keyed by ``(sprint_session_id, attempt)`` rather than by console session: a console
    session spawns one sprint session per backlog story, and each of those can be reviewed
    up to ``MAX_REVIEW_RETRIES`` times. "The diff" is therefore never singular.
    """

    model_config = STRICT

    console_session_id: str | None = None
    sprint_session_id: str = Field(..., min_length=1)
    ticket_key: str = Field(default="")
    attempt: int = Field(default=0, ge=0)
    git_sha: str = ""
    captured_at: str = Field(default_factory=utc_now_iso)
    files: list[DiffFileSummary] = Field(default_factory=list)
    total_additions: int = Field(default=0, ge=0)
    total_deletions: int = Field(default=0, ge=0)
    # True when files were dropped from this snapshot, not when one file was shortened.
    truncated: bool = False


class DiffSnapshotRef(BaseModel):
    """One entry in a console session's snapshot list — the story/attempt picker."""

    model_config = STRICT

    sprint_session_id: str = Field(..., min_length=1)
    attempt: int = Field(default=0, ge=0)
    ticket_key: str = ""
    captured_at: str = ""
    files_changed: int = Field(default=0, ge=0)
    total_additions: int = Field(default=0, ge=0)
    total_deletions: int = Field(default=0, ge=0)


class FileDecision(BaseModel):
    """One file's verdict in a human review pass (roadmap M7).

    A reject carries the reason into the Coder's next prompt — that is the whole feature,
    which is why an empty one is a validation error rather than a nudge in the UI.
    """

    model_config = STRICT

    path: str = Field(..., min_length=1)
    decision: Literal["accept", "reject"]
    reason: str | None = None
    # Server-stamped on record; a client-supplied value is overwritten.
    decided_at: str = Field(default_factory=utc_now_iso)

    @model_validator(mode="after")
    def _rejection_states_why(self) -> FileDecision:
        if self.decision == "reject" and not (self.reason or "").strip():
            raise ValueError("a rejected file needs a reason — the retry is built from it")
        return self


class DiffReviewState(BaseModel):
    """The open (or last) human review of one snapshot.

    ``undecided_paths`` is what a client shows next to its submit control: submitting
    accepts them, so hiding the count would ship files nobody looked at.
    """

    model_config = STRICT

    sprint_session_id: str = Field(..., min_length=1)
    attempt: int = Field(default=0, ge=0)
    # How many rejection rounds this run had already spent when the review opened.
    rejection_round: int = Field(default=0, ge=0)
    # "expired" is closed *without* a decision — the timeout lapsed, or the run was stopped.
    status: Literal["pending", "decided", "expired"] = "pending"
    requested_at: str = ""
    expires_at: str = ""
    decided_at: str | None = None
    decisions: list[FileDecision] = Field(default_factory=list)
    undecided_paths: list[str] = Field(default_factory=list)


class DiffDecisionsRequest(BaseModel):
    """``POST /v1/console/sessions/{id}/diff/decisions``.

    Decisions accumulate across calls (idempotent per path, last write wins) so partial
    review survives a reload; ``submit`` is what releases the run. Undecided files are
    accepted at submit — an explicit user action, which is why the count is published.
    """

    model_config = STRICT

    decisions: list[FileDecision] = Field(default_factory=list)
    # Which snapshot these decisions are for. Null means the open review, which is what a
    # client following ``ConsoleDiffPage.review`` always wants.
    sprint_session_id: str | None = None
    attempt: int | None = None
    submit: bool = False


class ConsoleDiffPage(BaseModel):
    """``GET /v1/console/sessions/{id}/diff``.

    ``snapshot`` is null before the first review pass — a normal state for most of a run,
    not an error, which is why this is a 200 and not a 404.
    """

    model_config = STRICT

    snapshot: WorkspaceDiffSnapshot | None = None
    available: list[DiffSnapshotRef] = Field(default_factory=list)
    # The review blocking the run, if one is open (M7). Null for a run that is not parked;
    # a client polling this reads it as "no action needed from me".
    review: DiffReviewState | None = None
