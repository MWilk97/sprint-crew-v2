"""What may be uploaded, and what it actually is (roadmap M11, ADR 0019).

Two checks rather than one. The declared media type decides whether an upload is allowed
at all; the bytes decide what it really is. A declared type is a claim by the client, and
a file announcing ``image/png`` that is not a PNG must never reach the vision tower —
that is the cheapest place a hostile upload can be stopped.

The allowlist is small on purpose. Only the Interpreter ever sees an attachment (ADR
0013), and it can do exactly two things with one: look at an image, or read text.
"""

from __future__ import annotations

from sprint_crew.schemas.attachment import AttachmentKind

_IMAGE_MEDIA_TYPES: frozenset[str] = frozenset(
    {"image/png", "image/jpeg", "image/webp", "image/gif"}
)
_TEXT_MEDIA_TYPES: frozenset[str] = frozenset(
    {
        "text/plain",
        "text/markdown",
        "text/csv",
        "application/json",
        "application/x-yaml",
        "text/yaml",
    }
)

ALLOWED_MEDIA_TYPES: frozenset[str] = _IMAGE_MEDIA_TYPES | _TEXT_MEDIA_TYPES

# Enough bytes for every signature below; a rejected upload should not be read further
# than it takes to reject it.
_SNIFF_BYTES = 16


class UnsupportedAttachmentError(ValueError):
    """The upload is not something the Interpreter may be shown."""


def classify(media_type: str) -> AttachmentKind | None:
    normalized = media_type.split(";")[0].strip().lower()
    if normalized in _IMAGE_MEDIA_TYPES:
        return AttachmentKind.IMAGE
    if normalized in _TEXT_MEDIA_TYPES:
        return AttachmentKind.TEXT
    return None


def normalize_media_type(media_type: str) -> str:
    """Drop the charset parameter and case. ``text/plain; charset=utf-8`` is ``text/plain``."""
    return media_type.split(";")[0].strip().lower()


def sniff_image_media_type(data: bytes) -> str | None:
    """The image type the bytes actually are, or None if they are not a known image."""
    head = data[:_SNIFF_BYTES]
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if head.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if head.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image/webp"
    return None


def _is_utf8_text(data: bytes) -> bool:
    # A NUL byte is the cheap tell for "this is binary wearing a text label"; UTF-8 decode
    # catches the rest. Both matter: the excerpt goes into a prompt as text.
    if b"\x00" in data:
        return False
    try:
        data.decode("utf-8")
    except UnicodeDecodeError:
        return False
    return True


def verify(declared_media_type: str, data: bytes) -> tuple[str, AttachmentKind]:
    """Return the verified ``(media_type, kind)`` or raise.

    For an image the sniffed type wins over the declared one — they must agree, and it is
    the bytes that are authoritative. For text there is nothing to sniff, so the check is
    that it decodes at all.
    """
    normalized = normalize_media_type(declared_media_type)
    kind = classify(normalized)
    if kind is None:
        raise UnsupportedAttachmentError(
            f"media type {normalized or '(none)'} is not accepted; "
            f"allowed: {', '.join(sorted(ALLOWED_MEDIA_TYPES))}"
        )
    if kind is AttachmentKind.IMAGE:
        actual = sniff_image_media_type(data)
        if actual is None:
            raise UnsupportedAttachmentError(
                f"content is not a recognised image despite declaring {normalized}"
            )
        if actual != normalized:
            raise UnsupportedAttachmentError(f"content is {actual} but was declared {normalized}")
        return actual, kind
    if not _is_utf8_text(data):
        raise UnsupportedAttachmentError(f"content declared {normalized} is not valid UTF-8 text")
    return normalized, kind
