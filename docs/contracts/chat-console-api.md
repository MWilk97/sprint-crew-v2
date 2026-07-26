# Chat console API contract

**Status: Implemented (durable sessions, SSE timeline, queued + cancellable runs).** The `/v1/console/*` routes are live in [console.py](../../src/sprint_crew/api/console.py), mounted alongside the existing `/sprint/*` and `/health` API (see [app.py](../../src/sprint_crew/api/app.py)). A separate off-GX UI repo (see [archive/HISTORY.md](../archive/HISTORY.md)) can target these routes directly. Machine-readable spec: [chat-console.openapi.yaml](chat-console.openapi.yaml). Pydantic models: `src/sprint_crew/schemas/console.py`.

Implementation status / MVP limitations:

- Sessions are **durable in SQLite** (M1): a session id stays valid across an API restart, and terminal sessions are reaped after `CONSOLE_SESSION_TTL_DAYS`. Per-session locking is process-local, so the single-worker assumption still holds.
- **Clarify questions are model-generated** by the Interpreter on the Work lane ([ADR 0013](../adr/0013-interpreter-clarify.md)). Question count and wording vary by request; `clarify_questions` may legitimately be **empty**, in which case the session opens directly in `ready`. Clients must not assume a fixed set of `question_id`s.
- **Fallback:** when the Work lane is cold or the Interpreter call fails, the backend serves the older deterministic questions instead (`q-scope`, `q-tests`, and `q-compat` when the prompt mentions an API/endpoint/route). These have `recommended_suggestion_id: null` and `why_asked: null`, which is how a client can tell the two apart. Clarify never fails the request.
- **Attachments (files/images) are not implemented yet.** The Interpreter model is multimodal and the endpoints are designed in ADR 0013, but no attachment routes exist in this contract version.
- `POST /start` with `mode=code` reuses the same orchestration as `POST /sprint/from-prompt` (which remains unchanged for direct callers); the clarify answers are appended to the prompt.
- **Start is non-blocking (M5).** `POST /start` returns `202` as soon as the run is queued. Workspace clone, vector indexing, context enrichment, the Work-lane load and the ScrumMaster call all happen after the response and report on the event stream. Previously the request blocked for the whole planning phase.
- **One run at a time (M5).** The GPU serialises runs, so a second start lands in `queued` with a `queue_position`. See [Run queue and cancel](#run-queue-and-cancel-m5).
- **`POST /cancel` really cancels (M5).** A queued run is dropped; a running run is asked to stop at its next checkpoint. See [Cancel](#post-v1consolesessionsidcancel).
- Progress mirroring: `GET /sessions/{id}` refreshes a queued or running code-mode session from the run registry and the backlog run (`sprint_ref.sprint_session_ids`, `queue_position`, completed/failed/cancelled status); clients may also poll `GET /sprint/backlog/{run_id}` directly.

Notes:

- **Auth:** when `CONSOLE_API_TOKEN` is set, every `/v1/console/*` and `/sprint/*` request requires `Authorization: Bearer <token>`. Empty or unset token disables auth (keeps unit tests and `smoke_cycle` working). `GET /health` is always open for probes.
- **Timeline (M2/M3):** `GET /v1/console/sessions/{id}/events?since=&limit=` serves one merged event stream per console session with a monotonic `seq` cursor by **polling** — see [Events](#get-v1consolesessionsidevents). `GET /v1/console/sessions/{id}/stream` serves the **same payload over SSE** (M3) — see [Stream](#get-v1consolesessionsidstream). A timeline built against polling carries over to SSE unchanged; the only difference is transport. `WebSocket` is not planned.
- `target_language` is a nullable placeholder for Phase 3 language-specialized lanes; senders should pass `null`.

## Auth (M0)

```http
Authorization: Bearer <CONSOLE_API_TOKEN>
```

| Route family | Auth when token set |
|--------------|---------------------|
| `/v1/console/*` | required → `401` without / wrong bearer |
| `/sprint/*` | required → `401` without / wrong bearer |
| `GET /health` | always open (probes / lane status) |

CORS: browser origins come from `CONSOLE_CORS_ORIGINS` (comma-separated; default `*` for local dev). Credentials are not used — send the token in `Authorization`, not cookies.

**SSE auth exception (M3):** browser `EventSource` cannot set an `Authorization` header, so `GET .../stream` also accepts the token as a `?token=<CONSOLE_API_TOKEN>` query parameter. The header is still accepted and preferred everywhere; use the query form only for the stream. A query-string token can appear in access logs — acceptable for the single-user LAN deployment this contract targets.

Related sprint/backlog status strings the UI may see when polling. `cancelled` is set on both since M5:

- Sprint session: `pending | running | awaiting_human | failed | approved | cancelled`
- Backlog run: `pending | running | completed | failed | cancelled`

## Session state machine

```mermaid
stateDiagram-v2
  [*] --> collecting: POST /sessions
  collecting --> clarifying: Interpreter emits clarify questions
  collecting --> ready: Interpreter has no questions
  clarifying --> ready: all questions answered
  ready --> queued: POST /start (after confirm)
  ready --> running: POST /start when the run slot is free
  queued --> running: run admitted
  queued --> cancelled: POST /cancel
  running --> completed
  running --> failed
  running --> cancelled: POST /cancel, once the run unwinds
  collecting --> cancelled: POST /cancel
  clarifying --> cancelled: POST /cancel
  ready --> cancelled: POST /cancel
```

Statuses: `collecting | clarifying | ready | queued | running | completed | failed | cancelled`. A session can reach `ready` without ever passing through `clarifying` — clients must drive off `status`, not off having seen questions. Confirmation is a separate boolean (`confirmed`) set by `POST /confirm` while `ready`; `POST /start` is rejected until `status == "ready"` and `confirmed == true`. Per [ADR 0012](../adr/0012-plan-code-modes-and-clarify.md), no sprint run ever starts from a raw prompt alone.

`queued` and `running` are both "started" for the purposes of `POST /messages`, which returns `409` in either — mid-run steering is out of scope, and accepting a message that changes nothing would be worse than a rejection. A session may also go straight from `ready` to `running` when nothing is ahead of it; clients must handle both.

## Run queue and cancel (M5)

One run executes at a time. This is a hardware constraint, not a policy: loading a lane stops every other lane, so two concurrent runs would only thrash lane swaps. The queue makes the constraint visible.

- `queue_position` — how many runs must finish before this one starts. Non-null only while `queued`, where it is `1` or more; `null` once admitted or once the session ends. A `run_queued` event carries the same number.
- **Cancel is asynchronous for a running run.** `POST /cancel` sets `cancel_requested_at`, emits a `cancel_requested` event, and returns `200` with `status` still `running`. The Stop button needs a pending state. The status becomes `cancelled` once the run actually unwinds.
- **Cancel is immediate otherwise.** A session that never started, or whose run is still queued, goes straight to `cancelled`.
- **How long "stopping" takes.** The run stops at its next checkpoint: between backlog stories, at a graph node boundary, or between Coder turns. A checkpoint is invisible while a subprocess is mid-call, so the honest worst case is bounded by `ACCEPTANCE_TEST_TIMEOUT_S` (900 s) and `run_command`'s own timeout (300 s). After `CANCEL_GRACE_S` (default 30 s) the run's task is hard-cancelled; lane teardown still completes, because it runs outside the cancelled task.
- **Restart.** Runs live in `asyncio` tasks, so a restart kills them. On startup every `pending`/`running` backlog run, `running` sprint session, and `queued`/`running` console session is marked `failed` with `interrupted by restart`. Nothing resumes.

Modes: `plan` (analysis/backlog preview only — never ships, no branch/PR) and `code` (after start, maps to today's ship-to-PR pipeline ending at `awaiting_human` per ADR 0010).

## Shared shapes

`ClarifyQuestion` — one open point the backend wants settled before a run. `recommended_suggestion_id` is the answer the backend would pick if the user said "just decide"; a client may render it preselected and let the user accept everything in one action. `why_asked` names the ambiguity that prompted the question. Both are null on fallback questions.

```json
{
  "question_id": "q-1",
  "text": "Should /metrics require authentication?",
  "why_asked": "The service has no auth layer today and metrics are usually scraped anonymously",
  "suggestions": [
    {
      "suggestion_id": "s-1-1",
      "label": "No auth",
      "detail": "src/sprint_crew/api/app.py",
      "rationale": "matches Prometheus scraper defaults; no new wiring"
    },
    {
      "suggestion_id": "s-1-2",
      "label": "Reuse the existing API auth",
      "detail": null,
      "rationale": "safer if the endpoint is public, but pulls auth into a new module"
    }
  ],
  "allow_custom": true,
  "recommended_suggestion_id": "s-1-1"
}
```

Question and suggestion ids are assigned by the backend (`q-<n>`, `s-<n>-<m>`) and are stable **within a session** only. `recommended_suggestion_id`, when non-null, always matches one of the listed `suggestion_id`s.

`IntentSummary` — what the Interpreter understood, echoed back so the user can correct it before confirming. Null when the fallback path produced the questions:

```json
{
  "restated_goal": "Add a /metrics endpoint exposing per-route request counters",
  "assumptions": ["Prometheus text format", "no persistence between restarts"],
  "unknowns": ["authentication"],
  "confidence": 0.7
}
```

`ClarifyAnswer` — exactly one of `selected_suggestion_id` or `custom_text` (`custom_text` is valid only when the question has `allow_custom: true`):

```json
{"question_id": "q-scope", "selected_suggestion_id": "s-api", "custom_text": null}
```

```json
{"question_id": "q-scope", "selected_suggestion_id": null, "custom_text": "only the CLI entry points"}
```

Errors use FastAPI's envelope: `{"detail": "<message>"}` with 400 (invalid transition/body semantics), 404 (unknown session), 409 (state conflict, e.g. start before confirm), 422 (schema validation).

## Endpoints

### POST /v1/console/sessions

Create a console session. Request:

```json
{
  "mode": "code",
  "initial_prompt": "Add a /metrics endpoint with request counters",
  "repo_url": "https://github.com/example/service",
  "target_language": null
}
```

Response `201` — full session object. With no `initial_prompt` the status is `collecting`. With one, the backend runs the Interpreter immediately and returns either `clarifying` (questions to answer, `intent` populated) or `ready` (nothing ambiguous — still requires `POST /confirm` before `/start`):

```json
{
  "session_id": "cs-7f3a",
  "mode": "code",
  "status": "clarifying",
  "confirmed": false,
  "repo_url": "https://github.com/example/service",
  "target_language": null,
  "messages": [
    {"role": "user", "content": "Add a /metrics endpoint with request counters", "timestamp": "2026-07-16T09:00:00+00:00"}
  ],
  "intent": null,
  "clarify_questions": [
    {
      "question_id": "q-scope",
      "text": "Which part of the repo should change?",
      "why_asked": null,
      "recommended_suggestion_id": null,
      "suggestions": [
        {"suggestion_id": "s-scope-focused", "label": "Only the files needed for this change"},
        {"suggestion_id": "s-scope-broad", "label": "Related modules too", "detail": "including tests and docs touched by the change"}
      ],
      "allow_custom": true
    }
  ],
  "clarify_answers": [],
  "sprint_ref": null,
  "plan_result": null,
  "error": null,
  "created_at": "2026-07-16T09:00:00+00:00",
  "updated_at": "2026-07-16T09:00:00+00:00"
}
```

Errors: `422` unknown/extra fields (`extra="forbid"`), invalid `mode`.

### GET /v1/console/sessions/{id}

Return the full session object (same shape as above). The UI polls this for state transitions. Errors: `404` unknown session.

### POST /v1/console/sessions/{id}/messages

Append a user chat message; the backend may respond with assistant messages and/or move to `clarifying`.

```json
{"content": "It should also cover the backlog endpoints"}
```

Response `200`: updated session. Errors: `404`; `409` if the session has already started (`queued` or `running`) or is terminal; `422` empty content.

### POST /v1/console/sessions/{id}/clarify

Submit answers to pending clarify questions.

```json
{
  "answers": [
    {"question_id": "q-scope", "selected_suggestion_id": "s-scope-focused", "custom_text": null},
    {"question_id": "q-tests", "selected_suggestion_id": null, "custom_text": "unit tests only, no live tiers"}
  ]
}
```

Response `200`: updated session — `status: "ready"` once every pending question is answered, otherwise still `clarifying`. Errors: `404`; `400` unknown `question_id`, both/neither answer fields set, or `custom_text` for a question with `allow_custom: false`; `409` if not in `clarifying`.

### POST /v1/console/sessions/{id}/confirm

Explicit user confirmation of the clarified request. Empty body. Response `200`: session with `confirmed: true` (status stays `ready`). Errors: `404`; `409` if `status != "ready"`.

### POST /v1/console/sessions/{id}/start

Queue the run. Empty body. Allowed only when `status == "ready"` and `confirmed == true`; response **`202`** with `status: "queued"` or `"running"`.

`202`, not `200`: the run has been accepted, not performed. It returns in milliseconds and everything expensive — clone, index, enrichment, lane load, ScrumMaster, then the batch — happens afterward and reports on the event stream.

- **mode = code**: the backend drives today's from-prompt pipeline. The session gains a `sprint_ref` for progress polling (see mapping below), plus `queue_position` when something is ahead of it:

```json
{
  "status": "queued",
  "queue_position": 1,
  "sprint_ref": {"backlog_run_id": "run-91c2", "sprint_session_ids": []}
}
```

- **mode = plan**: analysis only; on completion `status: "completed"` and `plan_result` is set — nothing is shipped:

```json
{
  "plan_result": {
    "summary": "Two stories: metrics endpoint, backlog counters",
    "stories": [
      {"title": "Expose /metrics with request counters", "rationale": "observability baseline"},
      {"title": "Count backlog endpoint hits", "rationale": null}
    ]
  }
}
```

Errors: `404`; `409` not ready or not confirmed (e.g. `{"detail": "session must be confirmed before start"}`).

### POST /v1/console/sessions/{id}/cancel

Cancel from any non-terminal state. Empty body. Response `200`. Errors: `404`; `409` if already `completed`, `failed`, or `cancelled`.

Two outcomes, distinguished by the response body rather than the status code — one code per route keeps generated clients simple:

| Situation | Response |
|---|---|
| Nothing started, or the run is still `queued` | `status: "cancelled"` — terminal immediately |
| The run is executing | `status: "running"` with `cancel_requested_at` set — Stop accepted, not yet done |

In the second case a `cancel_requested` event goes out immediately and the status flips to `cancelled` when the run unwinds. See [Run queue and cancel](#run-queue-and-cancel-m5) for how long that takes and why it cannot be instant.

```json
{"status": "running", "cancel_requested_at": "2026-07-26T09:15:22.481000+00:00"}
```

### GET /v1/console/sessions/{id}/events

The event timeline for a session, served by polling. One monotonic `seq` cursor spans **every** sprint session the console run spawned, so the client assembles the whole run from a single endpoint instead of walking `sprint_ref.sprint_session_ids` and polling `/sprint/session/{id}` per story.

Query params: `since` (default `0`, returns events with `seq` strictly greater), `limit` (default `500`, max `1000`). Response `200`:

```json
{
  "events": [
    {"seq": 1, "timestamp": "2026-07-25T09:00:01+00:00", "agent": "orchestrator", "event_type": "session_started", "phase": null, "level": "info", "summary": "Session started", "detail": null},
    {"seq": 2, "timestamp": "2026-07-25T09:04:12+00:00", "agent": "coder", "event_type": "tool_call", "phase": null, "level": "info", "summary": "apply_patch (ok)", "detail": {"tool": "apply_patch", "ok": true}}
  ],
  "next_seq": 2,
  "complete": false
}
```

Poll again with `since=next_seq` to drain the next page; every `seq` is delivered exactly once and never re-sent. `complete` reports whether the **session** reached a terminal status — not whether this page is the last. Keep polling until you receive an empty `events` array while `complete` is `true`. Errors: `404` unknown session.

Legacy sessions that ran before the events table existed are projected from their sprint sessions on first read, so old sessions stay renderable.

### GET /v1/console/sessions/{id}/stream

The same timeline as `GET .../events`, pushed over **Server-Sent Events** (M3) instead of polled. Each event frame carries the full `AgentEvent` JSON as `data`, with the SSE `id:` set to the event's `seq` so the browser resumes automatically after a dropped connection. Frames arrive on the default `message` channel — a plain `onmessage` handler receives every event and reads `event_type` from the body.

```
id: 1
data: {"seq":1,"timestamp":"2026-07-25T09:00:01+00:00","agent":"orchestrator","event_type":"session_started","phase":null,"level":"info","summary":"Session started","detail":null}

id: 2
data: {"seq":2,"timestamp":"2026-07-25T09:04:12+00:00","agent":"coder","event_type":"tool_call","phase":null,"level":"info","summary":"apply_patch (ok)","detail":{"tool":"apply_patch","ok":true}}

: ping

event: done
data:
```

- **Resume:** on reconnect the browser sends `Last-Event-ID: <seq>` automatically; the server replays every event with `seq` greater than it from the events table, then continues live — no gap, no duplicate. `?since=<seq>` is accepted as an explicit alternative (`Last-Event-ID` wins when both are present).
- **Heartbeat:** a `: ping` comment frame every `SSE_HEARTBEAT_S` seconds (default 15) keeps the connection alive through proxy idle-reap and long GPU silences. Ignore comment frames.
- **Termination:** when the run reaches a terminal state and all events are delivered, the server sends a named `event: done` frame and closes. The client should treat `done` as final and stop reconnecting.
- **Auth:** header or `?token=` (see [SSE auth exception](#auth-m0)).

Errors: `404` unknown session (returned before the stream opens, as an ordinary JSON response).

The events table is the source of truth; the stream is a live view over it. A slow client whose buffer overflows is dropped and is expected to reconnect and replay from its last `seq` — so a client must always be prepared to resume, and the polling endpoint remains a valid fallback.

**Closed event vocabulary.** `event_type` is drawn from a documented closed set — the `EventType` enum in [chat-console.openapi.yaml](chat-console.openapi.yaml), which is the single source of truth and is pinned against `schemas/session.py` by `tests/unit/test_docs_examples.py`. It is deliberately not restated here: this paragraph used to carry its own copy of the list and fell four milestones behind.

The wire type stays a plain string on purpose: **a client must render an unknown `event_type` generically, never reject the event** — each milestone adds types (M4 added `lane_loading`/`phase_started`, M5 added `run_queued`, `run_started`, `workspace_ready`, `backlog_planned`, `cancel_requested`, `cancelled`) and an older client must not break on them. `phase` and `level` (`debug | info | warning | error`) are present for filtering.

## Mapping to the current /sprint/* API

| Console (MVP) | Related `/sprint/*` endpoint | Notes |
|--------------------|-------------------------------|-------|
| `POST /v1/console/sessions`, `/messages`, `/clarify`, `/confirm` | — | New pre-run steps; no current equivalent |
| `POST /v1/console/sessions/{id}/start` (mode=code) | `POST /sprint/from-prompt` | Runs only after clarify + confirm; existing endpoint unchanged |
| `POST /v1/console/sessions/{id}/start` (mode=plan) | — | Analysis only, never ships |
| Progress after Code start | `GET /sprint/backlog/{run_id}`, `GET /sprint/session/{session_id}` | Via `sprint_ref` id mapping below |
| Human merge approval | `POST /sprint/session/{session_id}/approve` | Unchanged; ADR 0010 gate stays |
| `POST /v1/console/sessions/{id}/cancel` | — | Cancels the underlying run too (M5); no `/sprint/*` equivalent |

**Id mapping for progress polling (Code mode):** after start, `ConsoleSession.sprint_ref.backlog_run_id` is a `run_id` for the existing `GET /sprint/backlog/{run_id}`. That `BacklogRun.session_ids` lists per-ticket sprint session ids, each usable with the existing `GET /sprint/session/{session_id}` (event timeline, PR URL, `awaiting_human`). `sprint_ref.sprint_session_ids` mirrors the backlog run's `session_ids` as they are created. Console session ids (`cs-*` here) and sprint session ids are distinct id spaces.
