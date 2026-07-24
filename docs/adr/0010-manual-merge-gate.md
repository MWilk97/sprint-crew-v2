# ADR 0010: Manual merge gate

## Status

Accepted

## Context

Sprint Crew agents can implement code, run tests, and open pull requests autonomously. Fully automated merges to `main` carry risk: subtle correctness bugs, scope creep, or failing tests in environments the agents did not exercise.

## Decision

Agents **never auto-merge** to the default branch. After a successful review cycle, session status becomes `awaiting_human`. A human reviews the PR and merges manually.

The gate predicate itself is defined in [AGENTS.md §8.1](../../AGENTS.md) and implemented in
`sprint_crew.orchestrator.merge_gate.review_accepted`. This ADR governs only what happens
*after* it passes: a human, not an agent, merges.

## Consequences

- Every shipped change has a human checkpoint
- CI on the target repo remains the final authority for production quality
- Session API exposes PR URL and review outcome for operator review
