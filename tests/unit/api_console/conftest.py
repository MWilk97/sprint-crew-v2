"""Shared client and store-isolation fixtures for the console/API tests.

Six files in this directory each carried their own copy of the same three lines
(redirect SPRINT_SESSION_DB and SPRINT_WORKSPACE_BASE at tmp_path, clear the settings
cache, reset the console store) under four different names — fresh_console_store,
fresh_stores, isolated_stores, and an implicit reliance on the root autouse fixture.
They are one fixture; a test that needs more (an SSE heartbeat override, a run-registry
reset) layers its own on top rather than forking the whole thing.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from fastapi.testclient import TestClient
from httpx import ASGITransport, AsyncClient

from sprint_crew.api import console as console_module
from sprint_crew.api.app import app
from sprint_crew.config import get_settings


@pytest.fixture(autouse=True)
def fresh_stores(tmp_path, monkeypatch):
    """Console sessions are SQLite-backed; keep them off the real home DB and workspace
    tree so the suite stays hermetic."""
    monkeypatch.setenv("SPRINT_SESSION_DB", str(tmp_path / "console.db"))
    monkeypatch.setenv("SPRINT_WORKSPACE_BASE", str(tmp_path / "workspaces"))
    get_settings.cache_clear()
    console_module.reset_console_store()
    yield
    console_module.reset_console_store()
    get_settings.cache_clear()


@pytest.fixture
def client(auth_headers: dict[str, str]) -> TestClient:
    """Synchronous client. Auth headers are applied; tests that exercise auth itself
    build their own unauthenticated TestClient."""
    return TestClient(app, headers=auth_headers)


@pytest.fixture
async def api_client(auth_headers: dict[str, str]) -> AsyncIterator[AsyncClient]:
    """Async client, needed for anything that streams (SSE) or awaits background work."""
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test", headers=auth_headers
    ) as ac:
        yield ac
