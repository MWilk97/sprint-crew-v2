# ADR 0012: Plan/Code user modes and clarify-before-run

## Status

Accepted — partially implemented. The backend console API MVP (`/v1/console/*`,
including clarify and confirm) is live; the browser UI that consumes it is still
Proposed and lives in a separate off-GX repo (see [archive/HISTORY.md](../archive/HISTORY.md)).

## Context

Today a sprint can still start from a single POST and run to completion; the console
MVP adds steer-before-run. Users should choose how much the system does and refine
the request before an expensive GPU cycle.

Note: TechLead's internal planning modes (`template` / `static` / `tool_loop`) are orthogonal pipeline mechanics and are **not** the user modes defined here.

## Decision

Two user-facing modes:

- **Plan** — analysis and backlog only. Never ships: no branch, no PR, no repository writes.
- **Code** — the current ship-to-PR path (implement → test → review → PR → `awaiting_human`).

Before any sprint run in either mode, a **clarify** step presents suggested options (scope, target files, test expectations) and accepts a user-custom answer. The run starts only after an **explicit confirmation** from the user.

How those questions are produced is decided separately: [ADR 0013](0013-interpreter-clarify.md) replaced the original deterministic stub with the LLM Interpreter. The confirmation gate below is unchanged by that.

## Consequences

- No sprint run starts from a raw prompt alone; confirmation is mandatory
- Plan mode gives a cheap, side-effect-free way to explore a backlog before committing GPU time
- The manual merge gate ([ADR 0010](0010-manual-merge-gate.md)) is unchanged; Code mode still ends at `awaiting_human`
- The API exposes clarify options and confirmation state via `/v1/console/*` (MVP live; Phase history in [archive/HISTORY.md](../archive/HISTORY.md))
