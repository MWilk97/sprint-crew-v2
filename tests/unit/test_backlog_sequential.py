from __future__ import annotations

import subprocess

import pytest

from sprint_crew.orchestrator.session import prepare_chained_workspace


def _init_repo(tmp_path) -> None:
    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "test"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )
    readme = tmp_path / "README.md"
    readme.write_text("parent\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=tmp_path, check=True, capture_output=True)


def test_prepare_chained_workspace_copies_parent_and_creates_branch(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from sprint_crew.config import get_settings

    monkeypatch.setenv("SPRINT_WORKSPACE_BASE", str(tmp_path / "workspaces"))
    get_settings.cache_clear()

    parent = tmp_path / "parent"
    parent.mkdir()
    _init_repo(parent)
    (parent / "src").mkdir()
    (parent / "src" / "feature.py").write_text("done = True\n", encoding="utf-8")

    child = prepare_chained_workspace(parent, "child-session-12345678")
    assert child.exists()
    assert (child / "src" / "feature.py").read_text(encoding="utf-8") == "done = True\n"

    branch = subprocess.run(
        ["git", "branch", "--show-current"],
        cwd=child,
        capture_output=True,
        text=True,
        check=True,
    )
    assert branch.stdout.strip() == "feature/child-se"
