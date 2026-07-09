"""CPU embedding sidecar — OpenAI-compatible /v1/embeddings for sprint-crew."""

from __future__ import annotations

import logging
import os
from functools import lru_cache
from typing import Literal

from fastapi import FastAPI
from pydantic import BaseModel, Field

log = logging.getLogger("embed-sidecar")

MODEL_ID = os.environ.get("EMBED_MODEL_ID", "jinaai/jina-embeddings-v2-base-code")

app = FastAPI(title="Sprint Crew Embed Sidecar", version="0.1.0")


class EmbeddingRequest(BaseModel):
    input: str | list[str]
    model: str | None = None
    input_type: Literal["query", "passage"] = Field(
        default="passage",
        description="Jina code embeddings: query for search text, passage for indexed chunks.",
    )


class EmbeddingData(BaseModel):
    object: str = "embedding"
    index: int
    embedding: list[float]


class EmbeddingResponse(BaseModel):
    object: str = "list"
    data: list[EmbeddingData]
    model: str
    usage: dict[str, int]


@lru_cache(maxsize=1)
def _load_model():
    from sentence_transformers import SentenceTransformer

    log.info("Loading embedding model %s", MODEL_ID)
    return SentenceTransformer(MODEL_ID, trust_remote_code=True)


def _encode(texts: list[str], input_type: Literal["query", "passage"]) -> list[list[float]]:
    model = _load_model()
    kwargs: dict = {}
    prompts = getattr(model, "prompts", None) or {}
    if input_type == "query" and "query" in prompts:
        kwargs["prompt_name"] = "query"
    vectors = model.encode(texts, **kwargs)
    return [v.tolist() for v in vectors]


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "model": MODEL_ID}


@app.post("/v1/embeddings", response_model=EmbeddingResponse)
def embeddings(body: EmbeddingRequest) -> EmbeddingResponse:
    texts = [body.input] if isinstance(body.input, str) else list(body.input)
    if not texts:
        return EmbeddingResponse(data=[], model=MODEL_ID, usage={"prompt_tokens": 0, "total_tokens": 0})
    vectors = _encode(texts, body.input_type)
    data = [EmbeddingData(index=i, embedding=vec) for i, vec in enumerate(vectors)]
    token_est = sum(len(t.split()) for t in texts)
    return EmbeddingResponse(
        data=data,
        model=body.model or MODEL_ID,
        usage={"prompt_tokens": token_est, "total_tokens": token_est},
    )
