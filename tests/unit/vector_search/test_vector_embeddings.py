from __future__ import annotations

from unittest.mock import MagicMock, patch

import httpx
import pytest

from sprint_crew.config import get_settings
from sprint_crew.vector.embeddings import BATCH_SIZE, embed_texts


@pytest.fixture
def vector_enabled(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("VECTOR_INDEX_ENABLED", "true")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _response(vectors: list[list[float]], *, shuffle: bool = False) -> MagicMock:
    data = [{"index": i, "embedding": vec} for i, vec in enumerate(vectors)]
    if shuffle:
        data = list(reversed(data))
    resp = MagicMock()
    resp.json.return_value = {"data": data}
    resp.raise_for_status.return_value = None
    return resp


def _client_returning(responses: list) -> MagicMock:
    client = MagicMock()
    client.__enter__ = MagicMock(return_value=client)
    client.__exit__ = MagicMock(return_value=False)
    client.post.side_effect = responses
    return client


def test_embed_texts_raises_when_disabled() -> None:
    with pytest.raises(RuntimeError, match="disabled"):
        embed_texts(["hello"])


def test_embed_texts_empty_input_short_circuits(vector_enabled) -> None:
    assert embed_texts([]) == []


def test_embed_texts_orders_vectors_by_index(vector_enabled) -> None:
    client = _client_returning([_response([[1.0], [2.0]], shuffle=True)])
    with patch("sprint_crew.vector.embeddings.httpx.Client", return_value=client):
        vectors = embed_texts(["a", "b"])
    assert vectors == [[1.0], [2.0]]


def test_embed_texts_batches_large_input(vector_enabled) -> None:
    texts = [f"text {i}" for i in range(BATCH_SIZE + 1)]
    client = _client_returning(
        [
            _response([[float(i)] for i in range(BATCH_SIZE)]),
            _response([[99.0]]),
        ]
    )
    with patch("sprint_crew.vector.embeddings.httpx.Client", return_value=client):
        vectors = embed_texts(texts)
    assert client.post.call_count == 2
    assert len(vectors) == BATCH_SIZE + 1
    assert vectors[-1] == [99.0]


def test_embed_texts_retries_transient_http_error(vector_enabled) -> None:
    ok = _response([[1.0]])
    client = MagicMock()
    client.__enter__ = MagicMock(return_value=client)
    client.__exit__ = MagicMock(return_value=False)
    client.post.side_effect = [httpx.ConnectError("refused"), ok]
    with (
        patch("sprint_crew.vector.embeddings.httpx.Client", return_value=client),
        patch("sprint_crew.vector.embeddings.time.sleep") as sleep_mock,
    ):
        vectors = embed_texts(["a"])
    assert vectors == [[1.0]]
    assert client.post.call_count == 2
    sleep_mock.assert_called_once()


def test_embed_texts_raises_after_exhausted_retries(vector_enabled) -> None:
    client = MagicMock()
    client.__enter__ = MagicMock(return_value=client)
    client.__exit__ = MagicMock(return_value=False)
    client.post.side_effect = httpx.ConnectError("refused")
    with (
        patch("sprint_crew.vector.embeddings.httpx.Client", return_value=client),
        patch("sprint_crew.vector.embeddings.time.sleep"),
        pytest.raises(RuntimeError, match="Embedding request failed"),
    ):
        embed_texts(["a"])
