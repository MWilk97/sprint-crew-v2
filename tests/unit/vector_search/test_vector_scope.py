"""Two-tier collections: shared repo index plus a per-run overlay (roadmap M8)."""

from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from sprint_crew.vector.manifest import index_manifest
from sprint_crew.vector.scope import IndexScope, index_scope_for, overlay_hashes
from sprint_crew.vector.search import semantic_search
from sprint_crew.vector.store import collection_for_repo, repo_key


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("SPRINT_SESSION_DB", str(tmp_path / "manifest.db"))
    from sprint_crew.config import get_settings

    get_settings.cache_clear()
    root = tmp_path / "ws"
    root.mkdir()
    subprocess.run(["git", "init"], cwd=root, check=True, capture_output=True)
    (root / "a.py").write_text("x = 1\n", encoding="utf-8")
    (root / "b.py").write_text("y = 2\n", encoding="utf-8")
    yield root
    get_settings.cache_clear()


def test_scope_search_order_puts_the_overlay_first() -> None:
    scope = IndexScope(repo_key="k", shared="shared", overlay="overlay")
    assert scope.collections == ("overlay", "shared")
    assert IndexScope(repo_key="k", shared="shared").collections == ("shared",)


def test_scope_derives_the_repo_from_the_workspace_remote(workspace: Path) -> None:
    """A workspace names its own repo, so nothing has to thread repo_url through the graph."""
    subprocess.run(
        ["git", "remote", "add", "origin", "https://github.com/owner/repo.git"],
        cwd=workspace,
        check=True,
        capture_output=True,
    )
    scope = index_scope_for(workspace, run_scope_id="sprint-1")

    assert scope.repo_key == repo_key("git@github.com:owner/repo")
    assert scope.shared == collection_for_repo(scope.repo_key)
    assert scope.overlay == "code_chunks_run_sprint-1"


def test_overlay_is_every_file_differing_from_the_shared_baseline(workspace: Path) -> None:
    # Nothing indexed yet: the overlay degrades to a full index of the workspace, which is
    # exactly the pre-M8 per-run behaviour.
    assert sorted(overlay_hashes(workspace, "shared")) == ["a.py", "b.py"]

    digest = hashlib.sha256((workspace / "a.py").read_bytes()).hexdigest()
    index_manifest().apply(
        "shared",
        repo_key="k",
        git_sha="sha",
        upserted={"a.py": (digest, 1), "b.py": ("stale-hash", 1)},
        deleted=(),
    )

    # b.py differs from what the shared index holds — committed by a chained story or
    # edited by the Coder, the rule is the same.
    assert list(overlay_hashes(workspace, "shared")) == ["b.py"]


def _point(path: str, score: float) -> MagicMock:
    point = MagicMock()
    point.score = score
    point.payload = {
        "path": path,
        "start_line": 1,
        "end_line": 5,
        "chunk_kind": "code",
        "text": f"contents of {path}",
    }
    return point


def test_overlay_hits_supersede_the_shared_index_for_the_same_file(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A file the run has edited is only accurate in the overlay; the committed version
    of that same file must not come back alongside it."""
    monkeypatch.setenv("VECTOR_INDEX_ENABLED", "true")
    from sprint_crew.config import get_settings

    get_settings.cache_clear()

    results = {
        "overlay": [_point("edited.py", 0.6)],
        "shared": [_point("edited.py", 0.99), _point("untouched.py", 0.7)],
    }
    store = MagicMock()
    store.search.side_effect = lambda collection, *a, **k: results[collection]

    with (
        patch("sprint_crew.vector.search.QdrantStore", return_value=store),
        patch("sprint_crew.vector.search.embed_texts", return_value=[[0.1, 0.2]]),
    ):
        hits = semantic_search(("overlay", "shared"), "anything")

    by_path = [(h.path, h.score) for h in hits]
    # The shared 0.99 hit for edited.py is dropped despite outranking everything.
    assert by_path == [("untouched.py", 0.7), ("edited.py", 0.6)]
    get_settings.cache_clear()
