# Embedding sidecar (CPU)

OpenAI-compatible embedding API for sprint-crew vector indexing.

## Defaults

- Model: `jinaai/jina-embeddings-v2-base-code` (Apache 2.0)
- Port: `8080`

## API

- `GET /health` — liveness
- `POST /v1/embeddings` — body includes optional `input_type`: `query` | `passage` (Jina code model)

## Run via docker compose

```bash
./scripts/lane-ctl.sh start vector
```
