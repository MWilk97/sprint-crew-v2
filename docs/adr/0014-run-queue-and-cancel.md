# ADR 0014 — Run registry, a single-slot run queue, and cooperative cancel

**Status:** Accepted (2026-07-26). Implements roadmap milestone M5.
**Supersedes nothing.** Extends [ADR 0010](0010-manual-merge-gate.md) (no auto-merge) and
[ADR 0011/0012](0012-plan-code-modes-and-clarify.md) (console contract) without changing them.

## Context

Runs were dispatched with `BackgroundTasks.add_task` and no handle was kept. Three consequences:

1. **No cancel.** `POST /cancel` marked the console session `cancelled` and left the run
   churning — potentially a 40-minute GPU job. A live event feed makes a Stop button visually
   implied, and a Stop that does nothing is worse than no Stop.
2. **No queue.** A second concurrent run was admitted silently. `ensure_lane` stops every other
   lane before starting one, so two runs would thrash lane swaps against each other on shared
   unified memory.
3. **No restart honesty.** A row left `running` in SQLite lied forever; a UI polling it would
   spin on a run that no longer existed.

Separately, `POST /start` blocked for the whole planning phase — clone, vector index, context
enrichment, a Work-lane load with a 1200 s health budget, and the full ScrumMaster call all ran
inline before the response.

## Decision

### One run at a time, admitted through a registry

A process-global `RunRegistry` (`orchestrator/run_registry.py`) owns `asyncio.Task` handles and
admits runs through a one-permit semaphore. Waiting is **surfaced**, not hidden: a queued console
session reports `queue_position` (runs ahead of it) rather than pretending concurrency works.

`queue_position` counts runs ahead and is null unless `queued`. Counting raw queue index instead
would report a lone run as waiting on nothing, making every single run flicker `queued`→`running`
for one poll.

### Cancel the body, never the wrapper

`submit` creates a *wrapper* task that wins the slot and then awaits a separate *body* task.
`cancel` cancels the body. The wrapper is therefore never in a cancelled state and its `finally`
can freely `await` lane teardown.

This is the load-bearing detail. A cancelled task cannot reliably await anything, so teardown
inside a cancelled task can be interrupted half-done — and a lane left loaded pins a 23 GB model
on a shared 128 GB box for everything else. The run's own `finally` in `batch_cycle` still runs
first; the registry repeats the teardown from the wrapper because that first attempt is
best-effort. `stop_all_lanes` is idempotent, so repeating it is free.

### Cooperative first, hard escalation second

`CancelToken` propagates through a `ContextVar` — the same mechanism M4 already uses for the
event emitter, and it survives `asyncio.to_thread`. Checkpoints:

| Site | Behaviour |
|---|---|
| `_phased` graph-node entry | raise `RunCancelled` |
| top of the backlog story loop | raise `RunCancelled` |
| Coder turn / step / coverage loops | `break` — hand off partial work |
| before acceptance tests | skip |

The graph checks in exactly one place. `_phased` (added in M4) brackets every real node, and a
routing function runs *between* two phased nodes, so a cancel requested during routing is caught
on the next node's entry. The roadmap sketch called for a check in each routing function; that
predates `_phased` and would have meant a new graph node, three new edges, and three changed
route functions for no additional coverage.

Coder loops break rather than raise because edits are already on disk: handing off partial work
lets the run unwind through its normal path.

After `CANCEL_GRACE_S` (default 30 s) a watchdog hard-cancels the body. This exists for a body
blocked inside `subprocess.run`, which cannot see the token until the child returns.

### Both cancel exceptions must land on `cancelled`

`RunCancelled` is an ordinary `Exception`, so the broad `except Exception` handlers in
`session.py` and `batch_cycle.py` would have reported a user's Stop as a crash.
`asyncio.CancelledError` is a `BaseException`, so it skips those handlers entirely and would
leave the row stuck at `running`. Both now have an explicit arm ahead of the generic one, and
`CancelledError` is re-raised after persisting — swallowing it breaks asyncio's contract.

A story that already shipped keeps its place in `completed_session_ids`. The cancel check sits at
the *top* of the story loop, not after the cycle, so a Stop never retracts a PR that exists.

### Cancel is asynchronous, and says so

`POST /cancel` on a running run sets `cancel_requested_at`, emits `cancel_requested`, and returns
`200` with the status still `running`. Chosen over a new `cancelling` status: a nullable
timestamp is additive and does not break a client's state machine, and it works for polling as
well as for SSE.

Honest latency, documented in the contract: the run stops at its next checkpoint, and a
checkpoint is invisible while a subprocess is mid-call. The worst case is bounded by
`ACCEPTANCE_TEST_TIMEOUT_S` (900 s) and `run_command`'s own timeout (300 s) — not by
`CANCEL_GRACE_S`, which only governs when the task is hard-cancelled.

### Start returns 202

The handler allocates ids, persists a `pending` run, submits, and returns. Planning moved into
`orchestrator/prompt_run.py`, which installs the event emitter **before** the prep work — without
that, a non-blocking start would only replace a long wait with a silent one.

### Restart reconciles, never resumes

At startup every `pending`/`running` backlog run, `running` sprint session, and
`queued`/`running` console session is marked `failed` with `interrupted by restart`. Resuming
mid-graph would need checkpoint replay semantics we have not designed; making the stored state
honest is the whole requirement.

## Consequences

- `POST /start` changes from `200` to `202`; `POST /sprint/from-prompt` and `/from-ticket` too.
- New console status `queued`; new fields `queue_position` and `cancel_requested_at`.
- New events: `run_queued`, `run_started`, `workspace_ready`, `backlog_planned`,
  `cancel_requested`, `cancelled`.
- The Stop button needs a pending state.
- The registry is process-local. Multiple API workers would each keep their own queue and admit
  concurrently — the single-worker assumption from ADR 0011 now has teeth, not just a note.
- Mid-run steering stays out of scope. Nothing here forecloses it: a steering channel would add
  a state, not undo the queue.

## Invariants preserved

Merge gate stays deterministic; agents never auto-merge (ADR 0010); side effects stay in the
orchestrator; one lane loaded at a time — the queue enforces that rather than merely asserting
it; unit tests need no GPU, Qdrant, or credentials.
