from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from sprint_crew.vector.chunker import CodeChunk
from sprint_crew.vector.store import (
    QdrantStore,
    collection_for_repo,
    collection_for_run,
    normalize_repo_url,
    point_id,
    repo_key,
)


def _store_with_mock_client() -> tuple[QdrantStore, MagicMock]:
    client = MagicMock()
    with patch("sprint_crew.vector.store._client_for_url", return_value=client):
        store = QdrantStore(url="http://mock:6333")
    return store, client


def _chunk(path: str = "src/app.py") -> CodeChunk:
    return CodeChunk(
        path=path,
        start_line=1,
        end_line=10,
        chunk_kind="code",
        language="python",
        text="def app(): ...",
    )


def test_collection_names_sanitize_and_stay_distinct() -> None:
    assert collection_for_run("sess/../weird id!") == "code_chunks_run_sess_weird_id_"
    assert collection_for_repo("abc123") == "code_chunks_repo_abc123"


@pytest.mark.parametrize(
    "url",
    [
        "https://github.com/Owner/Repo.git",
        "git@github.com:owner/repo.git",
        "ssh://git@github.com/owner/repo/",
        "https://github.com/owner/repo",
    ],
)
def test_repo_key_collapses_equivalent_remotes(url: str) -> None:
    """One repo addressed four ways must land in one collection, or nothing is shared."""
    assert normalize_repo_url(url) == "github.com/owner/repo"
    assert repo_key(url) == repo_key("https://github.com/owner/repo")


def test_repo_key_separates_different_repos_and_defaults_to_fixture() -> None:
    assert repo_key("https://github.com/owner/other") != repo_key("https://github.com/owner/repo")
    assert repo_key(None) == repo_key("")


def test_point_id_is_stable_per_collection() -> None:
    chunk = _chunk()
    assert point_id("coll-a", chunk) == point_id("coll-a", chunk)
    assert point_id("coll-a", chunk) != point_id("coll-b", chunk)


def test_upsert_chunks_rejects_length_mismatch() -> None:
    store, _client = _store_with_mock_client()
    with pytest.raises(ValueError, match="length mismatch"):
        store.upsert_chunks("coll", [_chunk()], [])


def test_upsert_chunks_noop_on_empty() -> None:
    store, client = _store_with_mock_client()
    store.upsert_chunks("coll", [], [])
    client.upsert.assert_not_called()


def test_upsert_chunks_creates_collection_and_writes_payload() -> None:
    store, client = _store_with_mock_client()
    client.collection_exists.return_value = False
    store.upsert_chunks("coll", [_chunk()], [[0.1, 0.2]], git_sha="sha1")

    client.create_collection.assert_called_once()
    assert client.create_collection.call_args.kwargs["collection_name"] == "coll"
    points = client.upsert.call_args.kwargs["points"]
    assert len(points) == 1
    payload = points[0].payload
    assert payload["path"] == "src/app.py"
    assert payload["git_sha"] == "sha1"


def test_delete_by_paths_noop_without_paths_or_collection() -> None:
    store, client = _store_with_mock_client()
    store.delete_by_paths("coll", [])
    client.collection_exists.return_value = False
    store.delete_by_paths("coll", ["a.py"])
    client.delete.assert_not_called()


def test_delete_by_paths_filters_on_path() -> None:
    store, client = _store_with_mock_client()
    client.collection_exists.return_value = True
    store.delete_by_paths("coll", ["a.py", "b.py"])

    selector = client.delete.call_args.kwargs["points_selector"]
    condition = selector.filter.must[0]
    assert condition.key == "path"
    assert condition.match.any == ["a.py", "b.py"]


def test_list_collections_filters_by_prefix() -> None:
    store, client = _store_with_mock_client()
    mine, theirs = MagicMock(), MagicMock()
    mine.name = "code_chunks_repo_a"
    theirs.name = "other_thing"
    client.get_collections.return_value = MagicMock(collections=[mine, theirs])
    assert store.list_collections("code_chunks") == ["code_chunks_repo_a"]
