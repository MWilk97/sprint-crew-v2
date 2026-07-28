# ADR 0015 — A run parks on a human review before it ships

**Status:** Accepted (2026-07-28). Implements roadmap milestone M7.
**Extends** [ADR 0010](0010-manual-merge-gate.md) (no auto-merge) and
[ADR 0014](0014-run-queue-and-cancel.md) (single-slot queue, cooperative cancel). Changes
neither.

## Context

M6 gave the console a structured per-file diff to render. Rendering it is not reviewing it:
the user could watch the agent's change go past and had no way to say "not that file, and
here is why". Every path still ended at `awaiting_human` with a PR already open.

The obvious implementation — stage only the accepted paths and commit a subset — is a trap.
Acceptance tests and plan coverage are deterministic gates over the **whole tree**
(`merge_gate.py`, `plan_coverage.py`). Committing a subset can leave the tree red, leave
coverage unsatisfied, and open a PR that does not build. It would convert the project's
strongest property — gates you cannot argue with — into a guess.

## Decision

### Reject is feedback, not surgery

A rejection is recorded with a reason and fed into the retry machinery that already exists.
`format_user_rejection_feedback` writes the same `prior_review_feedback` slot the Reviewer's
findings use, the agent redoes those files, the gates re-run over the whole tree, and the
user reviews again. No partial-commit path is built, and none should be.

The rejection reason is therefore load-bearing, which is why an empty one is a 422 rather
than something the UI is trusted to enforce.

### The run parks; it does not end and resume

`awaitDiffReview` is a graph node on the `mergeGate → ship` edge. It waits on an
`asyncio.Event` that the decisions handler sets. The alternatives were LangGraph
`interrupt()`/resume — explicitly out of scope in the roadmap — and ending the run to resume
it later as a new one, which needs graph-state replay and workspace re-attachment to buy one
thing: releasing the admission slot.

Three consequences, all accepted deliberately:

- **A parked review holds the single run slot.** A second session's `/start` stays `queued`
  behind a human who went to lunch. Correct for a single-user box, and already visible
  through `queue_position`.
- **A parked review does not survive an API restart.** `sweep_interrupted_runs` marks it
  failed, exactly as it does any other in-flight run. Nothing is resumed here (ADR 0014).
- **It needs a timeout.** `DIFF_REVIEW_TIMEOUT_S` (default 1 h) is what stops an abandoned
  review from wedging the queue indefinitely. Expiry fails the run with reason
  `review_timeout`.

### Lanes are stopped at the park

Inside a backlog batch, `_stop_lane_after_cycle` deliberately no-ops so lanes survive between
stories. A review that sits for an hour would therefore idle a 23 GB model on a shared 128 GB
box. The gate calls `stop_all_lanes()` before waiting; a retry or the next story pays the
~60–120 s reload, which is cheaper than the idle (AGENTS.md §4.1).

### The gate only runs for console sessions

Gated on the context emitter, like the M6 capture it reads. A `/sprint/*` run, `smoke_cycle`,
and the live trap test have nobody who could answer, so parking would simply hang the cycle.
`CONSOLE_REVIEW_GATE_ENABLED=false` turns it off entirely.

### Rejection rounds are a budget of their own

`MAX_USER_REJECTION_ROUNDS` (default 3) is separate from `MAX_REVIEW_RETRIES` (4), or a picky
human would exhaust the machine's retries. `attempt` still advances on a rejection round — it
keys the diff snapshot, so freezing it would make the round overwrite the very capture the
user reviewed — and `route_after_gate` discounts the human's rounds instead.

Past the budget, the run **fails** with reason `review_budget_exhausted`. It does not ship a
tree the user said no to, and it does not loop.

### Decisions accumulate; submit releases

`POST .../diff/decisions` is idempotent per path, last write wins, so a half-finished review
survives a page reload and a user can change their mind. Only `submit: true` closes the
review, and it accepts whatever is still undecided — an explicit action, which is why
`undecided_paths` is published for the client to show on its submit control.

## Consequences

- The console gains its first **non-terminal state that blocks on the user**:
  `awaiting_review`. Clients must treat it as "your move", not as progress, and must not
  assume the status enum is closed.
- Two paths now end `failed` for non-technical reasons. Both carry a machine-readable
  `reason` on their terminal event (`review_timeout`, `review_budget_exhausted`) so a client
  branches on the slug rather than parsing error prose.
- Stop still works while parked, and is the manual escape hatch. The park is the only cancel
  checkpoint that runs while the graph is otherwise idle, so it reads the token itself.
- ADR 0010 is untouched: this adds a gate **before** the PR. Nothing merges.
- The deterministic merge gate is untouched: the human only ever sees a change the pipeline
  already accepted (AGENTS.md §8.1).

## Alternatives rejected

| Alternative | Why not |
|---|---|
| Commit only the accepted paths | Breaks whole-tree gates; can open a PR that does not build |
| LangGraph `interrupt()`/resume | Out of scope by roadmap decision; the park is smaller |
| End the run, resume as a new one | Needs state replay and workspace re-attach to save a slot one user is not contending for |
| Rejections consume `MAX_REVIEW_RETRIES` | Three picky rounds would leave the agent no retries for its own failures |
| No timeout | An abandoned review deadlocks the run queue until someone notices |
