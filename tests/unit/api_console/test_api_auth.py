"""Bearer auth for /v1/console/* and /sprint/*; GET /health stays open."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from sprint_crew.config import get_settings
from sprint_crew.orchestrator.session import SessionStore
from sprint_crew.schemas.session import (
    BacklogRunStatus,
    SessionStatus,
    SprintSession,
)


@pytest.fixture
def token_configured(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("CONSOLE_API_TOKEN", "test-secret")
    get_settings.cache_clear()
    yield "test-secret"
    get_settings.cache_clear()


def test_auth_disabled_when_token_empty(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CONSOLE_API_TOKEN", "")
    get_settings.cache_clear()
    try:
        resp = client.post(
            "/v1/console/sessions",
            json={"mode": "plan", "initial_prompt": None},
        )
        assert resp.status_code == 201
    finally:
        get_settings.cache_clear()


def test_requires_bearer_when_token_set(client: TestClient, token_configured: str, api_db) -> None:
    create = client.post(
        "/v1/console/sessions",
        json={"mode": "plan", "initial_prompt": None},
    )
    assert create.status_code == 401

    wrong = client.post(
        "/v1/console/sessions",
        json={"mode": "plan", "initial_prompt": None},
        headers={"Authorization": "Bearer wrong"},
    )
    assert wrong.status_code == 401

    ok = client.post(
        "/v1/console/sessions",
        json={"mode": "plan", "initial_prompt": None},
        headers={"Authorization": f"Bearer {token_configured}"},
    )
    assert ok.status_code == 201

    SessionStore(api_db).save(
        SprintSession(
            session_id="session-auth",
            status=SessionStatus.RUNNING,
            ticket_key="DEMO-1",
            workspace_root="/tmp/ws",
        )
    )
    sprint_denied = client.get("/sprint/session/session-auth")
    assert sprint_denied.status_code == 401

    sprint_ok = client.get(
        "/sprint/session/session-auth",
        headers={"Authorization": f"Bearer {token_configured}"},
    )
    assert sprint_ok.status_code == 200
    assert sprint_ok.json()["session_id"] == "session-auth"


def test_health_open_when_token_set(client: TestClient, token_configured: str) -> None:
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


def test_cancelled_enum_members_round_trip() -> None:
    assert SessionStatus("cancelled") is SessionStatus.CANCELLED
    assert BacklogRunStatus("cancelled") is BacklogRunStatus.CANCELLED
