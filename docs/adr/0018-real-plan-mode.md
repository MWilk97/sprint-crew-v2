# ADR 0018 — Real plan mode, and Promote as a child session

**Status:** Accepted (2026-07-28) · **Milestone:** M10 · **Builds on:** [0012](0012-plan-code-modes-and-clarify.md) (plan/code modes), [0014](0014-run-queue-and-cancel.md) (run slot)

## Context

ADR 0012 promised plan mode as "analysis and backlog only". What shipped was
`build_plan_result`: a heuristic that echoed the user's first message back as
`Implement: {prompt}` and each clarify answer as `Constraint: {answer}`. No model call, no
repository access, and it completed inside the request handler. The roadmap called it
theatre, which is fair — a user reading it would reasonably believe the system had
considered their repository, and it had not.

Making it real is mostly assembly: the ScrumMaster produces a `BacklogPlan` today and the
TechLead produces `TaskPlan`s read-only today. What needed deciding was the shape around
them — what a plan run costs, what it is allowed to touch, and what happens when the user
reads a plan and says go.

## Decision

### 1. Plan mode is a run, with everything that implies

It goes through the `RunRegistry` like a code run: one admission slot, queue position,
cooperative cancel, events on the session's existing timeline. This is not symmetry for its
own sake — plan mode needs the Work lane, and `ensure_lane` stops every other lane before
starting one. A plan run and a code run outside the same queue would thrash lane swaps.

The cost is the one genuinely breaking change in M10: `POST /start` on a plan session used
to return `completed` with a `plan_result` attached, and now returns `queued`/`running` with
`plan_result: null`. There is no way to keep the old shape and also make the work real — a
ScrumMaster call is tens of seconds and a `deep` plan is minutes. `contract_version` on
`ConsoleSession` is introduced here (2 = plan mode is asynchronous) so a client can branch
rather than flag-day.

### 2. Depth is the user's choice, not a server heuristic

`POST /start` takes an optional `{"depth": "quick"|"deep"}`.

`quick` runs the ScrumMaster alone: a backlog with titles, dependencies and heuristic
complexity, in tens of seconds. `deep` adds a read-only TechLead pass per story for real
`files_to_touch` / `steps` / `acceptance_tests` — a model run each.

A server-side cap ("detail the first N stories") was the obvious alternative and is worse:
the cost difference between the two is an order of magnitude, and which one a user wants
depends on whether they are sanity-checking scope or about to promote. Guessing that from
story count would be guessing about intent. The backlog is already capped at
`MAX_BACKLOG_STORIES` before planning starts, so `deep` is bounded without a second cap.

`ConsolePlanResult.depth` travels with the result. Without it, empty `files_to_touch` at
`quick` depth is indistinguishable from "the TechLead found no files to touch".

### 3. Plan mode builds its tickets locally — it must not reach Jira

`create_jira_tickets` (`orchestrator/backlog.py`) calls `jira.create_issue` per story. It is
the natural-looking way to turn `BacklogStory` into the `JiraTicket` the TechLead wants, and
using it would have made plan mode create real issues — in the one mode whose entire promise
is that reading a plan costs nothing outside this process. `ticket_from_story` builds the
same shape in memory. A test asserts the Jira path is never called.

The rest of the no-side-effects property holds structurally rather than by convention: the
TechLead already runs with `workspace_deps(mutate=False)` and `build_readonly_toolset`, and
plan mode uses the session's existing checkout rather than preparing a workspace of its own.

### 4. A story that fails to plan does not fail the plan

At `deep` depth, a TechLead exception for one story is logged and that story keeps its
ScrumMaster-level detail. The other four are still worth reading, and the gap is visible as
an empty `files_to_touch` beside a populated one.

`planning_mode` is reported per story for a related reason: `run_tech_lead` short-circuits to
`build_template_task_plan_validated` for tickets matching the template fixtures, and a
template plan is not repository analysis. Presenting one as though it were would be the same
class of lie M10 exists to remove.

### 5. Promote creates a child session and runs the stored backlog

`POST /sessions/{id}/promote` returns a **new** code-mode session, already confirmed and
queued, carrying `parent_session_id` back to the plan.

**Why not flip the same session.** A completed session is terminal, and the reaper,
`complete: true` on the events stream, and any client that stops polling on a terminal status
all depend on that. Reopening one would make "terminal" mean "terminal unless promoted".
Two sessions is also the truer model: a plan that was read and a run that executed it are two
things, and history should show both.

**Why the stored plan, not a re-plan.** The child runs the `BacklogPlan` the ScrumMaster
actually produced, persisted in `console_plans` alongside the prompt it came from and injected
into `run_from_prompt`. Re-planning would be less code, but a second ScrumMaster call can
return a different backlog, and then Promote would mean "run something like what you read".
Fidelity is the whole feature.

The click is the confirmation — the user has just read the backlog — so there is no separate
confirm/start step on the child.

## Consequences

- Plan mode now costs GPU time and queues behind code runs. It was free and instant; it is
  neither. `POST /start` still returns in milliseconds, so only the result is slower.
- A live plan run blocks `ask` on the same session (409, ADR 0017 §3) and blocks further
  messages. Correct — it holds the lane — but it is new behaviour for a mode that never held
  anything before.
- `ConsolePlanResult` gained fields but kept `summary` and `stories[].title`/`rationale`, so a
  result stored before M10 still validates and an old client still renders.
- Promote can be called repeatedly, producing several code sessions from one plan. Not
  guarded: planning once and running twice is a legitimate thing to want, and each child is
  independently visible.
- The stored backlog is dropped when the session is deleted or reaped, after which promote
  returns 409 rather than running a plan nobody can still read.
- Plan mode's terminal status is written by the run body rather than derived from a
  `BacklogRun` row, because it has none. `sync_plan_progress` therefore only derives queue
  position — the one place in the console where a status is owned rather than mirrored, and
  the reason is that there is nothing to mirror from.
