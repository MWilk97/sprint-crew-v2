"""Attachment upload, retrieval, validation and reclamation (roadmap M11)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from sprint_crew.config import get_settings
from sprint_crew.orchestrator.attachment_media import (
    UnsupportedAttachmentError,
    sniff_image_media_type,
    verify,
)
from sprint_crew.orchestrator.attachment_store import attachment_base, attachment_store, blob_path
from sprint_crew.orchestrator.console_store import (
    console_store,
    purge_console_session,
    reap_console_sessions,
)
from sprint_crew.schemas.attachment import AttachmentKind
from sprint_crew.schemas.console import ConsoleMode, ConsoleSession, ConsoleSessionStatus

# Only the signature is what the sniffer reads, so a header plus filler is a faithful
# stand-in for a real image and keeps the fixture readable.
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\rIHDR" + b"\x00" * 32
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 32
GIF = b"GIF89a" + b"\x00" * 32
WEBP = b"RIFF" + b"\x00\x00\x00\x00" + b"WEBP" + b"\x00" * 32


def _create(client: TestClient, prompt: str | None = "Fix the crash") -> dict:
    resp = client.post("/v1/console/sessions", json={"mode": "code", "initial_prompt": prompt})
    assert resp.status_code == 201
    return resp.json()


def _upload(
    client: TestClient,
    session_id: str,
    *,
    data: bytes = PNG,
    filename: str = "shot.png",
    media_type: str = "image/png",
):
    return client.post(
        f"/v1/console/sessions/{session_id}/attachments",
        files={"file": (filename, data, media_type)},
    )


def _terminal_session(session_id: str = "cs-done") -> ConsoleSession:
    return ConsoleSession(
        session_id=session_id,
        mode=ConsoleMode.CODE,
        status=ConsoleSessionStatus.COMPLETED,
    )


def _days_from_now(days: float) -> datetime:
    return datetime.now(tz=UTC) + timedelta(days=days)


# --- media verification -------------------------------------------------------------


@pytest.mark.parametrize(
    ("data", "expected"),
    [(PNG, "image/png"), (JPEG, "image/jpeg"), (GIF, "image/gif"), (WEBP, "image/webp")],
)
def test_sniffs_each_supported_image_signature(data: bytes, expected: str) -> None:
    assert sniff_image_media_type(data) == expected


def test_verify_accepts_text_with_a_charset_parameter() -> None:
    media_type, kind = verify("text/plain; charset=utf-8", b"boom at line 12")
    assert (media_type, kind) == ("text/plain", AttachmentKind.TEXT)


def test_verify_rejects_content_that_is_not_the_type_it_claims() -> None:
    """The declared type is a claim by the client; the bytes are the fact.

    This is the cheapest place a hostile upload is stopped — before anything reaches the
    vision tower.
    """
    with pytest.raises(UnsupportedAttachmentError, match="not a recognised image"):
        verify("image/png", b"just text, no signature")


def test_verify_rejects_one_image_type_declared_as_another() -> None:
    with pytest.raises(UnsupportedAttachmentError, match="is image/jpeg but was declared"):
        verify("image/png", JPEG)


def test_verify_rejects_binary_wearing_a_text_label() -> None:
    with pytest.raises(UnsupportedAttachmentError, match="not valid UTF-8"):
        verify("text/plain", b"log\x00\xff\xfe")


def test_verify_rejects_a_type_outside_the_allowlist() -> None:
    with pytest.raises(UnsupportedAttachmentError, match="not accepted"):
        verify("application/pdf", b"%PDF-1.7")


# --- upload -------------------------------------------------------------------------


def test_upload_stores_an_image_and_lists_it(client: TestClient) -> None:
    session = _create(client)
    resp = _upload(client, session["session_id"])

    assert resp.status_code == 201
    body = resp.json()
    assert body["kind"] == "image"
    assert body["media_type"] == "image/png"
    assert body["size_bytes"] == len(PNG)
    assert len(body["sha256"]) == 64

    listed = client.get(f"/v1/console/sessions/{session['session_id']}/attachments")
    assert [a["attachment_id"] for a in listed.json()["attachments"]] == [body["attachment_id"]]


def test_upload_accepts_a_text_log(client: TestClient) -> None:
    session = _create(client)
    resp = _upload(
        client,
        session["session_id"],
        data=b"Traceback (most recent call last):\n  boom\n",
        filename="run.log",
        media_type="text/plain",
    )
    assert resp.status_code == 201
    assert resp.json()["kind"] == "text"


def test_fetching_an_attachment_returns_the_bytes_and_its_type(client: TestClient) -> None:
    session = _create(client)
    uploaded = _upload(client, session["session_id"]).json()

    resp = client.get(
        f"/v1/console/sessions/{session['session_id']}/attachments/{uploaded['attachment_id']}"
    )

    assert resp.status_code == 200
    assert resp.content == PNG
    assert resp.headers["content-type"] == "image/png"


def test_identical_content_is_stored_once(client: TestClient) -> None:
    """Content-addressed: a retried upload must not double the session's quota."""
    session = _create(client)
    first = _upload(client, session["session_id"]).json()
    second = _upload(client, session["session_id"]).json()

    assert first["attachment_id"] == second["attachment_id"]
    assert (
        len(
            client.get(f"/v1/console/sessions/{session['session_id']}/attachments").json()[
                "attachments"
            ]
        )
        == 1
    )


def test_oversized_upload_is_413_and_leaves_a_reason_on_the_timeline(
    client: TestClient, monkeypatch
) -> None:
    monkeypatch.setenv("CONSOLE_ATTACHMENT_MAX_BYTES", "16")
    get_settings.cache_clear()
    session = _create(client)

    resp = _upload(client, session["session_id"], data=b"x" * 64, media_type="text/plain")

    assert resp.status_code == 413
    events = client.get(f"/v1/console/sessions/{session['session_id']}/events?since=0").json()
    rejected = [e for e in events["events"] if e["event_type"] == "attachment_rejected"]
    assert rejected and rejected[0]["level"] == "warning"


def test_mislabelled_upload_is_415(client: TestClient) -> None:
    session = _create(client)
    resp = _upload(client, session["session_id"], data=b"not an image at all")
    assert resp.status_code == 415


def test_disallowed_media_type_is_415(client: TestClient) -> None:
    session = _create(client)
    resp = _upload(
        client,
        session["session_id"],
        data=b"%PDF-1.7",
        filename="x.pdf",
        media_type="application/pdf",
    )
    assert resp.status_code == 415


def test_empty_upload_is_400(client: TestClient) -> None:
    session = _create(client)
    assert _upload(client, session["session_id"], data=b"").status_code == 400


def test_upload_to_a_terminal_session_is_409(client: TestClient) -> None:
    console_store().save(_terminal_session())
    assert _upload(client, "cs-done").status_code == 409


def test_upload_to_an_unknown_session_is_404(client: TestClient) -> None:
    assert _upload(client, "cs-nope").status_code == 404


def test_per_session_cap_is_409(client: TestClient, monkeypatch) -> None:
    monkeypatch.setenv("CONSOLE_MAX_ATTACHMENTS_PER_SESSION", "1")
    get_settings.cache_clear()
    session = _create(client)

    assert _upload(client, session["session_id"]).status_code == 201
    second = _upload(client, session["session_id"], data=JPEG, media_type="image/jpeg")
    assert second.status_code == 409


def test_uploads_are_disabled_by_the_kill_switch(client: TestClient, monkeypatch) -> None:
    session = _create(client)
    monkeypatch.setenv("CONSOLE_ATTACHMENTS_ENABLED", "false")
    get_settings.cache_clear()
    assert _upload(client, session["session_id"]).status_code == 503


# --- attaching to a message ---------------------------------------------------------


def test_a_message_carries_its_attachment_ids(client: TestClient) -> None:
    session = _create(client)
    uploaded = _upload(client, session["session_id"]).json()

    resp = client.post(
        f"/v1/console/sessions/{session['session_id']}/messages",
        json={"content": "here is the crash", "attachment_ids": [uploaded["attachment_id"]]},
    )

    assert resp.status_code == 200
    user_turns = [m for m in resp.json()["messages"] if m["role"] == "user"]
    assert user_turns[-1]["attachment_ids"] == [uploaded["attachment_id"]]


def test_a_message_with_an_unknown_attachment_is_400(client: TestClient) -> None:
    """Silently dropping the id would let a user send a screenshot nothing ever reads."""
    session = _create(client)
    resp = client.post(
        f"/v1/console/sessions/{session['session_id']}/messages",
        json={"content": "see this", "attachment_ids": ["at-nope"]},
    )
    assert resp.status_code == 400


def test_one_session_cannot_attach_anothers_upload(client: TestClient) -> None:
    owner = _create(client)
    other = _create(client)
    uploaded = _upload(client, owner["session_id"]).json()

    resp = client.post(
        f"/v1/console/sessions/{other['session_id']}/messages",
        json={"content": "borrowed", "attachment_ids": [uploaded["attachment_id"]]},
    )

    assert resp.status_code == 400
    assert (
        client.get(
            f"/v1/console/sessions/{other['session_id']}/attachments/{uploaded['attachment_id']}"
        ).status_code
        == 404
    )


# --- storage location and reclamation -----------------------------------------------


def test_blobs_live_outside_the_session_checkout(client: TestClient) -> None:
    """The workspace is a git checkout.

    An upload written into it would show up in `git status --porcelain` — which plan
    coverage reads — as a change the agent made.
    """
    session = _create(client)
    uploaded = _upload(client, session["session_id"]).json()

    stored = blob_path(session["session_id"], uploaded["sha256"])
    workspace_base = get_settings().workspace_base.resolve()

    assert stored.is_file()
    assert workspace_base not in stored.resolve().parents


def test_purge_removes_rows_and_blobs(client: TestClient) -> None:
    session = _create(client)
    uploaded = _upload(client, session["session_id"]).json()
    stored = blob_path(session["session_id"], uploaded["sha256"])

    purge_console_session(console_store().load(session["session_id"]))

    assert not stored.exists()
    assert attachment_store().list_for_session(session["session_id"]) == []


def test_reaper_removes_blobs_of_stale_sessions(client: TestClient) -> None:
    session = _create(client)
    uploaded = _upload(client, session["session_id"]).json()
    stored = blob_path(session["session_id"], uploaded["sha256"])

    stale = console_store().load(session["session_id"])
    stale.status = ConsoleSessionStatus.COMPLETED
    console_store().save(stale)
    reap_console_sessions(now=_days_from_now(30))

    assert not stored.exists()
    assert not (attachment_base() / session["session_id"]).exists()
