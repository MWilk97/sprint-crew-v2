"""Upload and fetch console attachments (roadmap M11, ADR 0019).

Uploads are allowed in any non-terminal state, including while a run holds the lane.
Storing bytes needs no model and no lane, so there is nothing here for a run to contend
with — the gate that matters is on ``POST /messages``, which already 409s once a run has
started, and duplicating it here would only stop a user staging a screenshot for the
message they intend to send next.
"""

from __future__ import annotations

import asyncio
from typing import Annotated

from fastapi import File, HTTPException, Response, UploadFile

from sprint_crew.api.console.state import (
    _TERMINAL_STATUSES,
    emit,
    require_session,
    router,
)
from sprint_crew.config import get_settings
from sprint_crew.orchestrator.attachment_media import UnsupportedAttachmentError, verify
from sprint_crew.orchestrator.attachment_store import attachment_store, content_hash
from sprint_crew.schemas.attachment import Attachment, AttachmentList
from sprint_crew.schemas.console import ConsoleSession
from sprint_crew.schemas.session import agent_event


async def store_attachment(attachment: Attachment, data: bytes) -> Attachment:
    return await asyncio.to_thread(attachment_store().save, attachment, data)


async def load_attachment(session_id: str, attachment_id: str) -> Attachment | None:
    return await asyncio.to_thread(attachment_store().get, session_id, attachment_id)


async def list_attachments(session_id: str) -> list[Attachment]:
    return await asyncio.to_thread(attachment_store().list_for_session, session_id)


async def resolve_attachments(session_id: str, attachment_ids: list[str]) -> list[Attachment]:
    return await asyncio.to_thread(attachment_store().get_many, session_id, attachment_ids)


async def count_attachments(session_id: str) -> int:
    return await asyncio.to_thread(attachment_store().count_for_session, session_id)


async def read_attachment_bytes(attachment: Attachment) -> bytes | None:
    return await asyncio.to_thread(attachment_store().read_blob, attachment)


@router.post("/sessions/{id}/attachments", response_model=Attachment, status_code=201)
async def upload_attachment(id: str, file: Annotated[UploadFile, File()]) -> Attachment:
    """Store one file against a session and return its metadata.

    Re-uploading identical content returns the existing attachment rather than a second
    copy — the store is content-addressed, and a client that retries a failed upload
    should not double the session's quota.
    """
    settings = get_settings()
    if not settings.console_attachments_enabled:
        raise HTTPException(status_code=503, detail="attachments are disabled")
    session = await require_session(id)
    if session.status in _TERMINAL_STATUSES:
        raise HTTPException(
            status_code=409,
            detail=f"session is {session.status.value}; attachments are no longer accepted",
        )

    data = await file.read()
    if not data:
        raise HTTPException(status_code=400, detail="attachment is empty")
    if len(data) > settings.console_attachment_max_bytes:
        # 413 rather than 400: the request was well formed, it was too big. Emitted as an
        # event too, so a user who watched an upload vanish can see why.
        await _reject(session, file.filename or "attachment", "too large", size_bytes=len(data))
        raise HTTPException(
            status_code=413,
            detail=(
                f"attachment is {len(data)} bytes; limit is {settings.console_attachment_max_bytes}"
            ),
        )
    if await count_attachments(id) >= settings.console_max_attachments_per_session:
        raise HTTPException(
            status_code=409,
            detail=(
                f"session already holds {settings.console_max_attachments_per_session} attachments"
            ),
        )
    try:
        media_type, kind = verify(file.content_type or "", data)
    except UnsupportedAttachmentError as exc:
        await _reject(session, file.filename or "attachment", str(exc), size_bytes=len(data))
        raise HTTPException(status_code=415, detail=str(exc)) from exc

    attachment = Attachment(
        session_id=id,
        filename=file.filename or "attachment",
        media_type=media_type,
        kind=kind,
        size_bytes=len(data),
        sha256=content_hash(data),
    )
    stored = await store_attachment(attachment, data)
    await emit(
        session,
        agent_event(
            "orchestrator",
            "attachment_uploaded",
            f"Attached {stored.filename}",
            attachment_id=stored.attachment_id,
            kind=stored.kind.value,
            media_type=stored.media_type,
            size_bytes=stored.size_bytes,
        ),
    )
    return stored


@router.get("/sessions/{id}/attachments", response_model=AttachmentList)
async def get_console_attachments(id: str) -> AttachmentList:
    await require_session(id)
    return AttachmentList(attachments=await list_attachments(id))


@router.get("/sessions/{id}/attachments/{attachment_id}")
async def get_console_attachment(id: str, attachment_id: str) -> Response:
    """The bytes back, so a client can render a thumbnail it did not keep."""
    await require_session(id)
    attachment = await load_attachment(id, attachment_id)
    if attachment is None:
        raise HTTPException(status_code=404, detail="Attachment not found")
    data = await read_attachment_bytes(attachment)
    if data is None:
        raise HTTPException(status_code=404, detail="Attachment content is no longer stored")
    return Response(
        content=data,
        media_type=attachment.media_type,
        headers={"Content-Disposition": f'inline; filename="{attachment.filename}"'},
    )


async def _reject(session: ConsoleSession, filename: str, reason: str, *, size_bytes: int) -> None:
    await emit(
        session,
        agent_event(
            "orchestrator",
            "attachment_rejected",
            f"Rejected {filename}",
            level="warning",
            reason=reason,
            size_bytes=size_bytes,
        ),
    )
