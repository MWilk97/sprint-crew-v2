from __future__ import annotations

import logging
import time
from typing import Literal

import httpx

from sprint_crew.config import get_settings

log = logging.getLogger(__name__)

InputType = Literal["query", "passage"]
BATCH_SIZE = 32
MAX_RETRIES = 3


def embed_texts(
    texts: list[str],
    *,
    input_type: InputType = "passage",
) -> list[list[float]]:
    settings = get_settings()
    if not settings.vector_index_enabled:
        raise RuntimeError("Vector index is disabled (VECTOR_INDEX_ENABLED=false).")
    if not texts:
        return []

    base = settings.embed_url.rstrip("/")
    url = f"{base}/embeddings"
    all_vectors: list[list[float]] = []

    for start in range(0, len(texts), BATCH_SIZE):
        batch = texts[start : start + BATCH_SIZE]
        vectors = _embed_batch(url, batch, input_type=input_type, model_id=settings.embed_model_id)
        all_vectors.extend(vectors)
    return all_vectors


def _embed_batch(
    url: str,
    texts: list[str],
    *,
    input_type: InputType,
    model_id: str,
) -> list[list[float]]:
    payload = {
        "input": texts,
        "model": model_id,
        "input_type": input_type,
    }
    last_error: Exception | None = None
    for attempt in range(MAX_RETRIES):
        try:
            with httpx.Client(timeout=120.0) as client:
                response = client.post(url, json=payload)
                response.raise_for_status()
                body = response.json()
            data = body.get("data", [])
            ordered = sorted(data, key=lambda item: item.get("index", 0))
            return [item["embedding"] for item in ordered]
        except (httpx.HTTPError, KeyError, TypeError) as exc:
            last_error = exc
            time.sleep(0.5 * (attempt + 1))
    assert last_error is not None
    raise RuntimeError(f"Embedding request failed: {last_error}") from last_error
