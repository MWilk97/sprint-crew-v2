from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from tests.helpers.from_prompt_live import VECTOR_INTEGRATION_PROMPT, VECTOR_TRAP_PROMPT
from tests.helpers.vector_fixtures import copy_vector_fixture, vector_fixture_root

from sprint_crew.orchestrator.complexity import (
    PromptComplexity,
    assess_prompt_complexity,
)
from sprint_crew.vector.chunker import count_indexable_files

_FIXTURE_ROOT = vector_fixture_root()
_PYTEST_BIN = Path(__file__).resolve().parents[2] / ".venv" / "bin" / "pytest"


def _collect_only(cwd: Path, target: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(_PYTEST_BIN), "--collect-only", "-q", target],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
    )


def test_vector_repo_fixture_has_enough_indexable_files() -> None:
    count = count_indexable_files(_FIXTURE_ROOT)
    assert count >= 20, f"expected >=20 indexable files, got {count}"


def test_grep_delivery_is_ambiguous_without_semantic_search() -> None:
    """Plain grep for 'delivery' must not uniquely identify ferry.py as the only hit."""
    proc = subprocess.run(
        [
            "grep",
            "-ril",
            "delivery",
            "--include=*.py",
            "--include=*.md",
            str(_FIXTURE_ROOT),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    hits = [line.strip() for line in (proc.stdout or "").splitlines() if line.strip()]
    assert len(hits) > 3, f"expected decoy delivery hits, got {hits}"
    ferry_hits = [h for h in hits if h.endswith("ferry.py")]
    assert len(ferry_hits) == 0 or hits[0] != ferry_hits[0], (
        "grep should not rank ferry.py first — semantic discovery required"
    )


def test_vector_trap_prompt_is_complex() -> None:
    assert assess_prompt_complexity(VECTOR_TRAP_PROMPT) == PromptComplexity.COMPLEX


def test_vector_integration_prompt_is_complex() -> None:
    assert assess_prompt_complexity(VECTOR_INTEGRATION_PROMPT) == PromptComplexity.COMPLEX


def test_story3_clean_overlay_collects_notify_tests(tmp_path: Path) -> None:
    workspace = copy_vector_fixture(tmp_path, overlay="story3_clean", name="story3-clean-check")
    proc = _collect_only(workspace, "tests/test_notify_routes.py")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "test_enqueue_notification_returns_id" in proc.stdout + proc.stderr


def test_base_fixture_notify_tests_fail_collection() -> None:
    """Documents stdlib platform shadow trap in vector_repo baseline."""
    proc = _collect_only(_FIXTURE_ROOT, "tests/test_notify_routes.py")
    assert proc.returncode != 0, "expected collection error on base vector_repo"
    combined = (proc.stdout + proc.stderr).lower()
    assert "error" in combined or "platform" in combined


def test_story3_clean_overlay_has_enough_indexable_files(tmp_path: Path) -> None:
    workspace = copy_vector_fixture(tmp_path, overlay="story3_clean", name="story3-index-check")
    count = count_indexable_files(workspace)
    assert count >= 20, f"expected >=20 indexable files, got {count}"


def test_queue_worker_is_stub_until_story1() -> None:
    import sys

    root = _FIXTURE_ROOT
    for entry in (root, root / "src"):
        path = str(entry)
        if path not in sys.path:
            sys.path.insert(0, path)

    from messaging.queue_worker import QueueWorker
    from storage.sqlite_repo import SqliteRepository

    repo = SqliteRepository(root / ".tmp-queue-worker-check.db")
    worker = QueueWorker(repo)
    with pytest.raises(NotImplementedError, match="story 1"):
        worker.process_once()


def test_ferry_queue_module_fails_on_unimplemented_fixture(tmp_path: Path) -> None:
    """Baseline fixture must fail queue tests until story 1 is implemented."""
    import shutil

    dest = tmp_path / "vector_repo"
    shutil.copytree(_FIXTURE_ROOT, dest, dirs_exist_ok=True)
    pytest_bin = Path(__file__).resolve().parents[2] / ".venv" / "bin" / "pytest"
    proc = subprocess.run(
        [str(pytest_bin), "-q", "tests/test_ferry_queue.py"],
        cwd=dest,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode != 0, proc.stdout + proc.stderr
