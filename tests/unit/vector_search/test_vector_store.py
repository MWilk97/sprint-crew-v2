from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from sprint_crew.vector.chunker import CodeChunk
from sprint_crew.vector.store import QdrantStore, collection_name, point_id


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


def test_collection_name_sanitizes_session_id() -> None:
    assert collection_name("sess/../weird id!") == "code_chunks_sess_weird_id_"


def test_point_id_is_stable_uuid() -> None:
    chunk = _chunk()
    assert point_id("s1", chunk) == point_id("s1", chunk)
    assert point_id("s1", chunk) != point_id("s2", chunk)


def test_get_collection_git_sha_none_when_collection_missing() -> None:
    store, client = _store_with_mock_client()
    client.collection_exists.return_value = False
    assert store.get_collection_git_sha("coll") is None


def test_get_collection_git_sha_none_when_no_points() -> None:
    store, client = _store_with_mock_client()
    client.collection_exists.return_value = True
    client.scroll.return_value = ([], None)
    assert store.get_collection_git_sha("coll") is None


def test_get_collection_git_sha_reads_payload() -> None:
    store, client = _store_with_mock_client()
    client.collection_exists.return_value = True
    point = MagicMock()
    point.payload = {"git_sha": "abc123"}
    client.scroll.return_value = ([point], None)
    assert store.get_collection_git_sha("coll") == "abc123"


def test_upsert_chunks_rejects_length_mismatch() -> None:
    store, _client = _store_with_mock_client()
    with pytest.raises(ValueError, match="length mismatch"):
        store.upsert_chunks("coll", "s1", [_chunk()], [])


def test_upsert_chunks_noop_on_empty() -> None:
    store, client = _store_with_mock_client()
    store.upsert_chunks("coll", "s1", [], [])
    client.upsert.assert_not_called()


def test_upsert_chunks_creates_collection_and_writes_payload() -> None:
    store, client = _store_with_mock_client()
    client.collection_exists.return_value = False
    store.upsert_chunks("coll", "s1", [_chunk()], [[0.1, 0.2]], git_sha="sha1")

    client.create_collection.assert_called_once()
    assert client.create_collection.call_args.kwargs["collection_name"] == "coll"
    points = client.upsert.call_args.kwargs["points"]
    assert len(points) == 1
    payload = points[0].payload
    assert payload["path"] == "src/app.py"
    assert payload["git_sha"] == "sha1"
    assert payload["session_id"] == "s1"
