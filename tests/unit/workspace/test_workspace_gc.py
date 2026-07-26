from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from sprint_crew.config import get_settings
from sprint_crew.orchestrator.session import collect_stale_workspaces


@pytest.fixture
def workspace_base(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("SPRINT_WORKSPACE_BASE", str(tmp_path))
    monkeypatch.setenv("WORKSPACE_TTL_DAYS", "14")
    get_settings.cache_clear()
    yield tmp_path
    get_settings.cache_clear()


def _age_dir(path: Path, days: float) -> None:
    past = time.time() - days * 86400.0
    os.utime(path, (past, past))


def test_collect_stale_workspaces_removes_old_keeps_fresh_and_current(
    workspace_base: Path,
) -> None:
    old = workspace_base / "old-session"
    fresh = workspace_base / "fresh-session"
    current = workspace_base / "current-session"
    for directory in (old, fresh, current):
        directory.mkdir()
    _age_dir(old, days=30)
    _age_dir(current, days=30)  # older than the TTL, but protected by `keep`

    removed = collect_stale_workspaces(keep="current-session")

    assert removed == ["old-session"]
    assert not old.exists()
    assert fresh.exists()
    assert current.exists()


def test_collect_stale_workspaces_noop_when_base_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SPRINT_WORKSPACE_BASE", str(tmp_path / "does-not-exist"))
    get_settings.cache_clear()
    try:
        assert collect_stale_workspaces() == []
    finally:
        get_settings.cache_clear()
