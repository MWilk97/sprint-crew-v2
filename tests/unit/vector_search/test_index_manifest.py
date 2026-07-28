"""The index manifest and its diff (roadmap M8).

Runs with neither Qdrant nor the embed sidecar: the whole point of keeping per-file
hashes in sqlite is that "what needs re-embedding" is answerable before either is touched.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from sprint_crew.vector.manifest import IndexManifest, plan_reindex


@pytest.fixture
def manifest(tmp_path: Path) -> IndexManifest:
    return IndexManifest(tmp_path / "manifest.db")


def test_plan_reindex_splits_added_changed_and_deleted() -> None:
    plan = plan_reindex(
        {"kept.py": "h1", "edited.py": "h2", "gone.py": "h3"},
        {"kept.py": "h1", "edited.py": "CHANGED", "new.py": "h4"},
    )

    assert plan.added == ("new.py",)
    assert plan.changed == ("edited.py",)
    assert plan.deleted == ("gone.py",)
    assert plan.to_embed == ("new.py", "edited.py")
    # A changed file is dropped as well as re-added: re-chunking can yield fewer chunks
    # than it had, and an upsert only overwrites the ids it writes.
    assert set(plan.to_drop) == {"edited.py", "gone.py"}


def test_plan_reindex_is_empty_when_nothing_moved() -> None:
    assert plan_reindex({"a.py": "h"}, {"a.py": "h"}).is_empty is True


def test_apply_round_trips_hashes_and_forgets_deleted(manifest: IndexManifest) -> None:
    manifest.apply(
        "coll",
        repo_key="k",
        git_sha="sha",
        upserted={"a.py": ("h1", 2), "b.py": ("h2", 3)},
        deleted=(),
    )
    assert manifest.file_hashes("coll") == {"a.py": "h1", "b.py": "h2"}
    assert manifest.chunk_count("coll") == 5

    manifest.apply(
        "coll",
        repo_key="k",
        git_sha="sha2",
        upserted={"a.py": ("h1-new", 1)},
        deleted=("b.py",),
    )
    assert manifest.file_hashes("coll") == {"a.py": "h1-new"}


def test_manifests_are_isolated_per_collection(manifest: IndexManifest) -> None:
    manifest.apply("one", repo_key="k", git_sha="s", upserted={"a.py": ("h", 1)}, deleted=())
    manifest.apply("two", repo_key="k", git_sha="s", upserted={"b.py": ("h", 1)}, deleted=())

    assert set(manifest.file_hashes("one")) == {"a.py"}
    assert set(manifest.file_hashes("two")) == {"b.py"}
    assert sorted(manifest.known_collections()) == ["one", "two"]


def test_evictable_returns_the_coldest_beyond_the_cap(manifest: IndexManifest) -> None:
    for name in ("first", "second", "third"):
        manifest.apply(name, repo_key="k", git_sha="s", upserted={}, deleted=())

    assert manifest.evictable(keep=3) == []
    # "first" was written longest ago, so it is the first to go.
    assert manifest.evictable(keep=2) == ["first"]
    assert manifest.evictable(keep=1) == ["first", "second"]


def test_touch_makes_a_collection_the_most_recently_used(manifest: IndexManifest) -> None:
    for name in ("first", "second"):
        manifest.apply(name, repo_key="k", git_sha="s", upserted={}, deleted=())

    manifest.touch("first")

    # Reads count as use, or a repo that is only ever searched would be evicted first.
    assert manifest.evictable(keep=1) == ["second"]


def test_forget_drops_files_and_the_collection_row(manifest: IndexManifest) -> None:
    manifest.apply("coll", repo_key="k", git_sha="s", upserted={"a.py": ("h", 1)}, deleted=())

    manifest.forget("coll")

    assert manifest.file_hashes("coll") == {}
    assert manifest.known_collections() == []
