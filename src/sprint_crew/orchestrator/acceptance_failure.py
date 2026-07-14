from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

from sprint_crew.paths import is_test_path as _shared_is_test_path

FailureKind = Literal[
    "none",
    "collection_error",
    "import_error",
    "syntax_error",
    "assertion_failure",
    "command_failed",
    "unknown",
]

_FILE_PATH_RE = re.compile(r'File "([^"]+\.py)"', re.IGNORECASE)
_PATH_LINE_RE = re.compile(r"^([\w./-]+\.py):\d+:", re.MULTILINE)
_E_LINE_RE = re.compile(r"^E\s+(.+)$", re.MULTILINE)

_COLLECTION_MARKERS = (
    "error collecting",
    "errors during collection",
    "collected 0 items",
)
_IMPORT_MARKERS = (
    "importerror",
    "modulenotfounderror",
    "cannot import name",
    "no module named",
)
_SYNTAX_MARKERS = ("syntaxerror",)
_ASSERTION_MARKERS = (
    "assertionerror",
    "assert ",
    "failed tests/",
    " failures",
)


@dataclass(frozen=True)
class AcceptanceFailureAnalysis:
    kind: FailureKind
    tester_can_help: bool
    source_paths: tuple[str, ...]
    test_paths: tuple[str, ...]
    summary: str
    detail_excerpt: str


def _normalize_repo_path(path: str) -> str:
    cleaned = path.replace("\\", "/")
    for marker in ("/testbed/", "/workspace/"):
        idx = cleaned.find(marker)
        if idx >= 0:
            cleaned = cleaned[idx + len(marker) :]
    if cleaned.startswith("./"):
        cleaned = cleaned[2:]
    return cleaned.lstrip("/")


def _is_test_path(path: str) -> bool:
    return _shared_is_test_path(_normalize_repo_path(path))


def _is_source_path(path: str) -> bool:
    return not _is_test_path(path)


def _extract_paths(output: str) -> tuple[tuple[str, ...], tuple[str, ...]]:
    source: set[str] = set()
    tests: set[str] = set()
    for match in _FILE_PATH_RE.finditer(output):
        normalized = _normalize_repo_path(match.group(1))
        if not normalized.endswith(".py"):
            continue
        if _is_test_path(normalized):
            tests.add(normalized)
        else:
            source.add(normalized)
    for match in _PATH_LINE_RE.finditer(output):
        normalized = _normalize_repo_path(match.group(1))
        if not normalized.endswith(".py"):
            continue
        if _is_test_path(normalized):
            tests.add(normalized)
        else:
            source.add(normalized)
    return tuple(sorted(source)), tuple(sorted(tests))


def _failure_excerpt(output: str, *, max_lines: int = 30) -> str:
    lines = output.strip().splitlines()
    if not lines:
        return ""
    start = 0
    for idx, line in enumerate(lines):
        low = line.lower()
        if any(marker in low for marker in _COLLECTION_MARKERS):
            start = max(0, idx - 2)
            break
        if any(marker in low for marker in _IMPORT_MARKERS + _SYNTAX_MARKERS):
            start = max(0, idx - 2)
            break
        if "traceback" in low:
            start = idx
            break
    excerpt = "\n".join(lines[start : start + max_lines]).strip()
    return excerpt


def _detect_kind(lowered: str, *, has_source: bool) -> FailureKind:
    if any(marker in lowered for marker in _COLLECTION_MARKERS):
        return "collection_error"
    if any(marker in lowered for marker in _SYNTAX_MARKERS):
        return "syntax_error"
    if any(marker in lowered for marker in _IMPORT_MARKERS):
        return "import_error"
    if any(marker in lowered for marker in _ASSERTION_MARKERS):
        return "assertion_failure"
    if "exit_code=" in lowered and "exit_code=0" not in lowered.split("exit_code=")[-1][:12]:
        return "command_failed"
    if has_source:
        return "unknown"
    return "none"


def _build_summary(
    kind: FailureKind,
    *,
    source_paths: tuple[str, ...],
    test_paths: tuple[str, ...],
) -> str:
    if kind == "none":
        return "Acceptance tests passed or no failure detected."
    if kind == "collection_error":
        if source_paths:
            return (
                "pytest could not collect tests because source code failed to import "
                f"({', '.join(source_paths)})."
            )
        return "pytest could not collect tests before running assertions."
    if kind == "import_error":
        if source_paths:
            return f"Import failed in source file(s): {', '.join(source_paths)}."
        if test_paths:
            return f"Import failed in test file(s): {', '.join(test_paths)}."
        return "Import error in acceptance test run."
    if kind == "syntax_error":
        if source_paths:
            return f"Syntax error in source file(s): {', '.join(source_paths)}."
        return "Syntax error blocked acceptance tests."
    if kind == "assertion_failure":
        return "Tests collected but assertions failed."
    if kind == "command_failed":
        return "Acceptance test command exited non-zero."
    if source_paths:
        return f"Acceptance tests failed with errors in source file(s): {', '.join(source_paths)}."
    return "Acceptance tests failed."


def _tester_can_help(
    kind: FailureKind,
    *,
    source_paths: tuple[str, ...],
    test_paths: tuple[str, ...],
) -> bool:
    if kind == "none":
        return False
    if source_paths:
        return False
    if kind == "assertion_failure":
        return True
    if test_paths:
        return True
    if kind in ("collection_error", "import_error", "syntax_error"):
        return False
    return kind != "command_failed"


def analyze_acceptance_output(output: str) -> AcceptanceFailureAnalysis:
    text = output.strip()
    if not text:
        return AcceptanceFailureAnalysis(
            kind="none",
            tester_can_help=False,
            source_paths=(),
            test_paths=(),
            summary="No acceptance test output.",
            detail_excerpt="",
        )

    lowered = text.lower()
    exit_codes = [int(match.group(1)) for match in re.finditer(r"exit_code=(\d+)", text)]
    if exit_codes and all(code == 0 for code in exit_codes):
        return AcceptanceFailureAnalysis(
            kind="none",
            tester_can_help=False,
            source_paths=(),
            test_paths=(),
            summary="Acceptance tests passed or no failure detected.",
            detail_excerpt="",
        )

    source_paths, test_paths = _extract_paths(text)
    kind = _detect_kind(lowered, has_source=bool(source_paths))
    if kind == "none" and re.search(r"exit_code=(?!0)\d+", lowered):
        kind = "command_failed"

    tester_can_help = _tester_can_help(
        kind,
        source_paths=source_paths,
        test_paths=test_paths,
    )
    summary = _build_summary(kind, source_paths=source_paths, test_paths=test_paths)
    detail_excerpt = _failure_excerpt(text)

    return AcceptanceFailureAnalysis(
        kind=kind,
        tester_can_help=tester_can_help,
        source_paths=source_paths,
        test_paths=test_paths,
        summary=summary,
        detail_excerpt=detail_excerpt,
    )
