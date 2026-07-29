"""Schemas for console attachments (roadmap M11, ADR 0019).

An attachment is untrusted input. These models carry only identity, provenance and size —
never the bytes, which live on disk under their own root keyed by content hash
(``orchestrator/attachment_store.py``).
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import Enum
from uuid import uuid4

from pydantic import BaseModel, Field

from sprint_crew.schemas._base import STRICT

#: A sha256 hex digest, which is what names a blob on disk.
_SHA256_CHARS = 64


def _utc_now_iso() -> str:
    return datetime.now(tz=UTC).isoformat()


def _attachment_id() -> str:
    return f"at-{uuid4().hex[:8]}"


class AttachmentKind(str, Enum):
    """How the Interpreter may consume it.

    ``image`` becomes a content part for the vision tower; ``text`` becomes a fenced,
    truncated excerpt. There is deliberately no third member: an upload the allowlist
    cannot place in one of these two is rejected rather than carried as an unknown.
    """

    IMAGE = "image"
    TEXT = "text"


class Attachment(BaseModel):
    model_config = STRICT

    attachment_id: str = Field(default_factory=_attachment_id, min_length=1)
    session_id: str = Field(..., min_length=1)
    filename: str = Field(..., min_length=1)
    #: The verified type, not the one the client declared — see ``attachment_media.verify``.
    media_type: str = Field(..., min_length=1)
    kind: AttachmentKind
    size_bytes: int = Field(..., ge=0)
    #: Content address: the blob's name on disk, and what makes pasting one screenshot
    #: twice cost one blob rather than two.
    sha256: str = Field(..., min_length=_SHA256_CHARS, max_length=_SHA256_CHARS)
    created_at: str = Field(default_factory=_utc_now_iso)


class AttachmentList(BaseModel):
    model_config = STRICT

    attachments: list[Attachment] = Field(default_factory=list)


class AttachmentPayload(BaseModel):
    """An attachment resolved to what a prompt can actually carry.

    The split exists so the agent layer never reads a blob: the orchestrator loads the
    bytes and turns them into either a data URL or a bounded excerpt, and the prompt
    builder only ever sees one of those two. Which one is decided by ``kind``, not by
    which field happens to be populated.
    """

    model_config = STRICT

    filename: str = Field(..., min_length=1)
    media_type: str = Field(..., min_length=1)
    kind: AttachmentKind
    #: ``data:<media_type>;base64,…`` for an image; empty for text.
    data_url: str = ""
    #: The excerpt for text; empty for an image.
    text: str = ""
