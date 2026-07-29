"""Turn stored attachments into something a prompt can carry (roadmap M11, ADR 0019).

The one place blobs are read for a model. Everything downstream sees an
``AttachmentPayload`` — a data URL or a bounded excerpt — so the agent layer never touches
the filesystem and an unreadable blob degrades to "skipped" rather than failing clarify.
"""

from __future__ import annotations

import base64
import logging
from collections.abc import Sequence

from sprint_crew.config import get_settings
from sprint_crew.orchestrator.attachment_media import excerpt_text
from sprint_crew.orchestrator.attachment_store import attachment_store
from sprint_crew.schemas.attachment import Attachment, AttachmentKind, AttachmentPayload

logger = logging.getLogger(__name__)


def load_payloads(attachments: Sequence[Attachment]) -> list[AttachmentPayload]:
    """Read each blob and render it for a prompt, dropping any that has gone missing.

    A reaped or hand-deleted blob must not fail the clarify round it happened to be
    attached to — the questions are still worth asking without it.
    """
    store = attachment_store()
    limit = get_settings().console_attachment_excerpt_bytes
    payloads: list[AttachmentPayload] = []
    for attachment in attachments:
        data = store.read_blob(attachment)
        if data is None:
            logger.warning("attachment %s has no stored blob", attachment.attachment_id)
            continue
        if attachment.kind is AttachmentKind.IMAGE:
            encoded = base64.b64encode(data).decode()
            payloads.append(
                AttachmentPayload(
                    filename=attachment.filename,
                    media_type=attachment.media_type,
                    kind=attachment.kind,
                    data_url=f"data:{attachment.media_type};base64,{encoded}",
                )
            )
            continue
        payloads.append(
            AttachmentPayload(
                filename=attachment.filename,
                media_type=attachment.media_type,
                kind=attachment.kind,
                text=excerpt_text(data, limit),
            )
        )
    return payloads
