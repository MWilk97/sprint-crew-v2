from __future__ import annotations

import os
import subprocess
from pathlib import Path

_DEFAULT_TIMEOUT_SECONDS = 30


def find_git_dir(workspace_root: Path) -> Path | None:
    standard = workspace_root / ".git"
    if standard.exists():
        return standard
    if workspace_root.name == "work_tree":
        split = workspace_root.parent / ".git"
        if split.exists():
            return split
    return None


def git_env(*, git_dir: Path, work_tree: Path) -> dict[str, str]:
    env = os.environ.copy()
    env["GIT_DIR"] = str(git_dir)
    env["GIT_WORK_TREE"] = str(work_tree)
    env["GIT_CEILING_DIRECTORIES"] = str(work_tree.parent)
    return env


def run_git(
    argv: list[str],
    *,
    work_tree: Path,
    git_dir: Path,
    timeout: int = _DEFAULT_TIMEOUT_SECONDS,
) -> tuple[int, str, str]:
    env = git_env(git_dir=git_dir, work_tree=work_tree)
    try:
        proc = subprocess.run(
            ["git", *argv],
            cwd=str(work_tree),
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"git {' '.join(argv)} timed out after {timeout}s") from exc
    return proc.returncode, proc.stdout, proc.stderr
