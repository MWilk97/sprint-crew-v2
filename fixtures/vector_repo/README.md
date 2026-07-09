# Mini Task Platform

Internal platform combining task CRUD, SQLite persistence, and an outbound notification
subsystem.

## Architecture

| Business term | Code location | Role |
|---------------|---------------|------|
| Message ferry | `src/messaging/ferry.py` | Outbound dispatch layer; hands messages to adapters |
| Adapter handoff | `src/messaging/adapters/` | SMTP and webhook delivery channels |
| Delivery resilience | `src/messaging/retry_policy.py` | Backoff when handoff fails |
| Outbound queue | `src/storage/sqlite_repo.py` + `src/messaging/queue_worker.py` | Persistent queue before dispatch |
| Task API | `src/api/routes.py` | HTTP CRUD for tasks |

The **ferry** is the central dispatch pipeline — not to be confused with HTTP retry
utilities in `src/utils/retry_http.py` (generic client retries only).

Notification REST endpoints belong alongside task routes in `src/api/routes.py`.

## Sprint stories (vector e2e fixture)

| Story | Scope |
|-------|--------|
| 1 | `sqlite_repo.py` outbound queue + `queue_worker.py` wiring to ferry (`tests/test_ferry_queue.py`) |
| 2 | `retry_policy.py` exponential backoff only (`tests/test_ferry_retry.py`) |
| 3 | Notification REST routes in `src/api/routes.py` (`tests/test_notify_routes.py`) |

Story 1 must not modify `retry_policy.py` or notification routes.

## Layout

```
src/
  api/routes.py          # task + notification HTTP handlers
  messaging/
    ferry.py             # OutboundHandoff, dispatch
    queue_worker.py      # drains sqlite queue through ferry (story 1)
    retry_policy.py      # exponential backoff policy (story 2)
    models.py            # Message, DeliveryStatus
    adapters/            # smtp, webhook
  storage/sqlite_repo.py # SQLite queue + task storage
  platform/config.py     # shared settings
```

## Test tiers

| Variant | Stories | Tier |
|---------|---------|------|
| Base `fixtures/vector_repo` (no `tests/conftest.py`) | 1–2 capability; story 3 **trap** (stdlib `platform` shadow) | `agent_capability` / `agent_trap` |
| Overlay `overlays/story3_clean/` (`tests/conftest.py` fixes import shadow) | Story 3 REST without trap | `agent_capability` story 3 clean |

Apply overlay at test time via `copy_vector_fixture(..., overlay="story3_clean")` in [`tests/helpers/vector_fixtures.py`](../../tests/helpers/vector_fixtures.py).
