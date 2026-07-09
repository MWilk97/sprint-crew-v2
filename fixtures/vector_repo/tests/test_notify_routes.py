"""Story 3 — REST endpoints to enqueue notifications and query delivery status."""

from __future__ import annotations

import json
import threading
from http.client import HTTPConnection
from pathlib import Path

import pytest

from api.routes import TaskHandler, HTTPServer
from platform.config import PlatformConfig
from storage.sqlite_repo import SqliteRepository


@pytest.fixture
def api_server(tmp_path: Path):
    db = tmp_path / "api.db"
    import api.routes as routes_mod

    routes_mod._repo = SqliteRepository(db)
    server = HTTPServer(("127.0.0.1", 0), TaskHandler)
    host, port = server.server_address
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield host, port
    server.shutdown()


def _post_json(host: str, port: int, path: str, payload: dict) -> tuple[int, dict]:
    conn = HTTPConnection(host, port, timeout=5)
    body = json.dumps(payload).encode()
    conn.request("POST", path, body=body, headers={"Content-Type": "application/json"})
    resp = conn.getresponse()
    data = json.loads(resp.read().decode() or "{}")
    conn.close()
    return resp.status, data


def _get_json(host: str, port: int, path: str) -> tuple[int, dict]:
    conn = HTTPConnection(host, port, timeout=5)
    conn.request("GET", path)
    resp = conn.getresponse()
    data = json.loads(resp.read().decode() or "{}")
    conn.close()
    return resp.status, data


def test_enqueue_notification_returns_id(api_server) -> None:
    host, port = api_server
    status, data = _post_json(
        host,
        port,
        "/notifications",
        {"recipient": "a@b.com", "body": "alert", "channel": "smtp"},
    )
    assert status == 201
    assert "id" in data


def test_get_notification_status(api_server) -> None:
    host, port = api_server
    _, created = _post_json(
        host,
        port,
        "/notifications",
        {"recipient": "a@b.com", "body": "alert"},
    )
    msg_id = created["id"]
    status, data = _get_json(host, port, f"/notifications/{msg_id}")
    assert status == 200
    assert data["status"] in {"pending", "dispatched", "delivered", "failed"}
