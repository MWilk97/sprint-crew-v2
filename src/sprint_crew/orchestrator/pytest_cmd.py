from __future__ import annotations

import shutil
from pathlib import Path

from sprint_crew.config import get_settings


def resolve_pytest_bin(workspace_root: Path | None = None) -> str:
    candidates: list[Path] = []
    if workspace_root is not None:
        candidates.append(workspace_root / ".venv" / "bin" / "pytest")
    candidates.append(get_settings().project_root / ".venv" / "bin" / "pytest")
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate)
    found = shutil.which("pytest")
    return found or "pytest"


def normalize_test_command(cmd: str, workspace_root: Path) -> str:
    stripped = cmd.strip()
    if stripped == "pytest" or stripped.startswith("pytest "):
        pytest_bin = resolve_pytest_bin(workspace_root)
        return stripped.replace("pytest", pytest_bin, 1)
    return cmd
