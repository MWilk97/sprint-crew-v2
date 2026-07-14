"""Shared path predicates and parsers.

Leaf module: orchestrator, vector, agents, and tools all import from here;
this module imports nothing above ``tools._safety``.
"""

from __future__ import annotations

import re
from pathlib import Path

from sprint_crew.tools._safety import FORBIDDEN_PATH_SEGMENTS

INDEXABLE_SUFFIXES = frozenset(
    {
        ".py",
        ".md",
        ".yaml",
        ".yml",
        ".toml",
        ".json",
        ".sh",
        ".js",
        ".ts",
        ".tsx",
        ".jsx",
        ".go",
        ".rs",
        ".txt",
    }
)

SKIP_SUFFIXES = frozenset({".lock", ".min.js", ".map", ".png", ".jpg", ".gif", ".woff", ".woff2"})

_PATH_RE = re.compile(
    r"[\w./-]+\.(?:py|js|ts|tsx|jsx|go|rs|md|yaml|yml|json|toml|txt|sh)",
    re.IGNORECASE,
)

DIFF_PATH_RE = re.compile(r"^diff --git a/(.+?) b/", re.MULTILINE)


def normalize_path(path: str) -> str:
    return path.replace("\\", "/").lstrip("./")


def is_test_path(path: str) -> bool:
    normalized = normalize_path(path)
    return normalized.startswith("tests/") or "/tests/" in normalized


def paths_in_text(text: str) -> list[str]:
    """Ordered, de-duplicated path-like strings found in free text."""
    seen: set[str] = set()
    paths: list[str] = []
    for match in _PATH_RE.findall(text):
        normalized = match.lstrip("./")
        if normalized not in seen:
            seen.add(normalized)
            paths.append(normalized)
    return paths


def paths_from_diff(diff_text: str) -> list[str]:
    """Ordered, de-duplicated file paths from ``diff --git`` headers."""
    seen: set[str] = set()
    paths: list[str] = []
    for match in DIFF_PATH_RE.finditer(diff_text):
        path = normalize_path(match.group(1))
        if path and path not in seen:
            seen.add(path)
            paths.append(path)
    return paths


def should_skip_path(rel: Path) -> bool:
    """True for paths excluded from indexing/manifest (caches, binaries, non-code)."""
    for part in rel.parts:
        if part in FORBIDDEN_PATH_SEGMENTS:
            return True
    suffix = rel.suffix.lower()
    if suffix in SKIP_SUFFIXES:
        return True
    return bool(suffix) and suffix not in INDEXABLE_SUFFIXES


def chunk_kind_for_path(rel: str) -> str:
    normalized = rel.replace("\\", "/")
    if normalized.startswith("tests/") or "/tests/" in normalized or normalized.startswith("test_"):
        return "test"
    if normalized.endswith((".md", ".rst")):
        return "doc"
    if normalized.endswith((".yaml", ".yml", ".toml", ".json")):
        return "config"
    return "code"
