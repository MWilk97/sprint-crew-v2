# ADR 0017 — Codebase chat: a read-only Explainer that holds the run slot

**Status:** Accepted (2026-07-28) · **Milestone:** M9 · **Builds on:** [0014](0014-run-queue-and-cancel.md) (run slot), [0016](0016-durable-repo-index.md) (warm index)

## Context

M8 gave every console session a checkout and a warm repository index from the moment it is
created. Nothing consumed either: clarify still ran blind, and there was no way to ask the
repository anything without opening a ticket and starting a run.

The pieces for a read-only agent already existed and had exactly one caller. The questions
this record settles are not "how do we build it" but four things the mechanism does not
decide for us: how an answer reaches the client, how it is serialised against runs, where
citations come from, and whether the Work lane stays warm between questions.

## Decision

### 1. The answer streams on the timeline, not on a channel of its own

`POST /ask` returns 202 and the answer arrives as events on the existing SSE stream:

```
ask_started → tool_call* → answer_delta* → citation* → answer_complete
```

every event carrying `detail.message_id`.

A console session already has one ordered, replayable, cursor-addressed event stream. Giving
chat its own would make "the agent read this file while answering" and "the agent read this
file during a run" two incompatible shapes for the same fact, and would double the transport
surface for a client that has already built one.

**`answer_complete` is authoritative; deltas are a preview.** The two can legitimately differ
— a model that emits a paragraph and then decides to call another tool has already streamed
text that is not the final answer. A client renders deltas as they arrive and then *replaces*
the bubble with `detail.text`. This is also what makes reconnect correct: `Last-Event-ID`
replays the deltas, and a client that appends would double the answer while one that replaces
converges.

**Deltas are coalesced**, one event per `ANSWER_DELTA_CHARS` or `ANSWER_DELTA_INTERVAL_S`,
whichever trips first. Per-token events would be one SQLite row and one bus publish each.

### 2. Token streaming here does not contradict the roadmap's ban on it

The roadmap puts token-level streaming out of scope, and that ruling is about
`structured_completion`: guided JSON with `strict: true` and a repair-retry loop, where
streaming would mean reworking parsing for every reporter agent. The Explainer produces prose
through a plain pydantic-ai `Agent`, so none of that applies. Deltas are gated on
`FinalResultEvent` so intermediate tool-calling turns do not leak scratch text into the answer.

### 3. An ask takes the single run slot, and is refused while a run is live

Both, and they are different concerns.

**It takes the slot** because `ensure_lane` stops every other lane before starting one. A run
admitted midway through an ask would take the Work lane away from it and the answer would die
in a connection error. Holding the existing one-permit admission queue is the cheapest correct
fix, and it makes Stop work with no new machinery.

**It is refused (409) rather than queued** when a run is already live. Single-user by
decision, and someone who asks a question expects an answer in seconds; parking it behind a
forty-minute run would be worse than saying no. Rejecting is also honest — the client can say
why. `ask_in_flight` is derived from the registry on every read, never stored, so a restart
that killed the task does not leave a composer disabled forever.

Cancelling an ask is deliberately a *separate* endpoint from cancelling the session:
abandoning a question is not a reason to throw away the conversation it was asked in.

### 4. Citations are derived, never requested

They come from the Explainer's tool log — the files it actually opened, with the line ranges
it read — intersected with the `path:line` references the answer's own prose contains. A model
asked to cite itself invents line numbers; the files it opened are a fact. A path named in the
answer that was never touched and does not exist on disk is dropped rather than rendered as a
broken link. An answer citing nothing falls back to what was opened, which is still a truthful
"here is what this was based on".

`GET /sessions/{id}/files/{path}` serves the cited file, through the same `resolve_safe_path`
guard every agent tool uses. Without it a citation is inert text.

### 5. No warm lane (roadmap appendix question 2)

A cold ask pays the lane load — minutes — and says so, since `ensure_lane` already emits
`lane_loading` / `lane_ready` (M4).

The alternative was an idle-timeout warm Work lane. It was rejected for now on grounds of
sequencing, not merit: AGENTS.md §4.1 is currently absolute ("do not keep lanes loaded 24/7;
start lanes on demand"), and an idle-timeout policy is compatible with its intent but is a
real amendment to a rule about a shared 128 GB box. Amending it belongs in its own session
with its own measurements — how often a second question follows within the timeout, and what
the resident cost is against concurrent GPU work — not bundled into the milestone that first
makes chat exist. **This is the one thing in M9 most likely to want revisiting once the
feature is in use**; the latency is the whole argument for it.

## Consequences

- Chat and clarify contend for one lane. Under the single-user assumption that is a
  scheduling non-problem, and a 409 says so plainly. It would not survive a second user.
- Answer text is stored twice: as a `ConsoleMessage` and inside the `answer_complete` event's
  detail. Deliberate — the message list must be readable without replaying the timeline, and
  the timeline must be renderable without loading the session.
- The Explainer reads repository content and returns it as prose. Repo content is fenced as
  data in its prompt and it holds no mutating tools, so a hostile file can mislead an answer
  but cannot cause an action. The stronger requirement in ADR 0013 — attachment content must
  never steer a run — is unaffected: nothing here reaches a run.
- First-ask latency on a cold lane is minutes. If that proves to be what stops the feature
  being used, revisit §5 rather than weakening §3.
