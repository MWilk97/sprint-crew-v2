from __future__ import annotations

import shutil
from pathlib import Path

from sprint_crew.config import get_settings
from tests.helpers.vector_ab import copy_fixture_workspace


def vector_fixture_root() -> Path:
    return get_settings().project_root / "fixtures" / "vector_repo"


def overlay_dir(overlay: str) -> Path:
    return vector_fixture_root() / "overlays" / overlay


def apply_vector_overlay(dest: Path, overlay: str) -> None:
    """Merge overlay files into a copied vector_repo workspace."""
    source = overlay_dir(overlay)
    if not source.is_dir():
        raise FileNotFoundError(f"vector fixture overlay not found: {source}")
    for path in source.rglob("*"):
        if path.is_dir():
            continue
        rel = path.relative_to(source)
        target = dest / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)


def copy_vector_fixture(
    tmp_path: Path,
    *,
    overlay: str | None = None,
    name: str | None = None,
) -> Path:
    """Copy vector_repo into tmp_path, optionally applying an overlay."""
    dest = copy_fixture_workspace(vector_fixture_root(), tmp_path, name=name)
    if overlay:
        apply_vector_overlay(dest, overlay)
    return dest
