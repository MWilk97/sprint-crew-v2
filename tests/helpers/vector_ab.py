from __future__ import annotations

import os
import subprocess
from pathlib import Path
from uuid import uuid4


def copy_fixture_workspace(
    fixture_repo_path: Path, tmp_path: Path, name: str | None = None
) -> Path:
    """Copy greeter fixture into tmp_path and init git (same as tmp_workspace fixture)."""
    dest = tmp_path / (name or f"workspace-{uuid4().hex[:8]}")
    subprocess.run(
        ["cp", "-a", str(fixture_repo_path), str(dest)],
        check=True,
        capture_output=True,
    )
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "sprint-crew",
        "GIT_AUTHOR_EMAIL": "sprint-crew@local",
        "GIT_COMMITTER_NAME": "sprint-crew",
        "GIT_COMMITTER_EMAIL": "sprint-crew@local",
    }
    subprocess.run(["git", "init"], cwd=dest, check=True, capture_output=True)
    subprocess.run(["git", "add", "-A"], cwd=dest, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "init workspace"],
        cwd=dest,
        check=True,
        capture_output=True,
        env=env,
    )
    return dest
