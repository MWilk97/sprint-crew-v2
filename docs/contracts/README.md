# API contracts

Contracts consumed by the future web console repo ([ADR 0011](../adr/0011-web-console-off-gx.md)): the UI talks only to this FastAPI backend, never to the vLLM lanes, so these files are the entire integration surface.

## Current (live today)

The implemented API is `/sprint/*` and `/health`, defined in [src/sprint_crew/api/app.py](../../src/sprint_crew/api/app.py) and listed in the [root README](../../README.md#api). There is no separate contract file for it yet; FastAPI serves its own OpenAPI at `/docs` when the backend runs.

## Proposed (not implemented)

| File | Contents |
|------|----------|
| [chat-console-api.md](chat-console-api.md) | Human-readable console API: state machine, endpoints with JSON examples, clarify/confirm shapes, mapping to `/sprint/*` |
| [chat-console.openapi.yaml](chat-console.openapi.yaml) | OpenAPI 3.1 spec aligned with the markdown; enough to generate a mock client |

Pydantic stubs matching these contracts live in `src/sprint_crew/schemas/console.py` (strict `extra="forbid"`, unit-tested, **not** wired into any route).

## How the UI repo consumes these

1. Generate or hand-write a client from [chat-console.openapi.yaml](chat-console.openapi.yaml); mock the server from its examples.
2. Follow the session flow from [chat-console-api.md](chat-console-api.md): create → messages → clarify → confirm → start; poll `GET /v1/console/sessions/{id}` (no SSE/WebSocket in this version).
3. After a Code-mode start, poll progress through the **existing** endpoints via `sprint_ref`: `GET /sprint/backlog/{run_id}`, then `GET /sprint/session/{session_id}` per ticket.
4. Treat everything under `/v1/console/` as Proposed until the roadmap Phase 1 routes actually land; the current `/sprint/*` endpoints are stable and unchanged.
