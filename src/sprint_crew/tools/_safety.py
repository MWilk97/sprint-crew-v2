from __future__ import annotations

from pathlib import Path

FORBIDDEN_PATH_SEGMENTS: frozenset[str] = frozenset(
    {
        ".git",
        ".venv",
        "venv",
        "__pycache__",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        "node_modules",
        ".crew-index",
    }
)

FORBIDDEN_PATH_SUBSTRINGS: frozenset[str] = frozenset({"<", ">"})


class UnsafePathError(ValueError):
    pass


def resolve_safe_path(rel: str, *, root: Path) -> Path:
    if not rel or not rel.strip():
        raise UnsafePathError("Path must be non-empty.")
    raw = Path(rel)
    if raw.is_absolute() or raw.drive:
        raise UnsafePathError(f"Absolute paths are not allowed: {rel!r}")
    bad = {p for p in raw.parts if p in FORBIDDEN_PATH_SEGMENTS}
    if bad:
        raise UnsafePathError(f"Path {rel!r} contains forbidden segment(s) {sorted(bad)}.")
    bad_chars = {
        ch for part in raw.parts for ch in part if ch in FORBIDDEN_PATH_SUBSTRINGS
    }
    if bad_chars:
        raise UnsafePathError(
            f"Path {rel!r} contains unresolved placeholder character(s) "
            f"{sorted(bad_chars)} -- did the planner emit a literal "
            f"'<ext>' / '<name>' instead of a concrete filename?"
        )
    root_resolved = root.resolve(strict=False)
    candidate = (root_resolved / raw).resolve(strict=False)
    try:
        candidate.relative_to(root_resolved)
    except ValueError as exc:
        raise UnsafePathError(f"Path {rel!r} escapes the root directory.") from exc
    return candidate
