# ADR 0010: Manual merge gate

## Status

Accepted

## Context

Sprint Crew agents can implement code, run tests, and open pull requests autonomously. Fully automated merges to `main` carry risk: subtle correctness bugs, scope creep, or failing tests in environments the agents did not exercise.

## Decision

Agents **never auto-merge** to the default branch. After a successful review cycle, session status becomes `awaiting_human`. A human reviews the PR and merges manually.

The merge gate in code requires:

```python
review.passed and review.tests_passed and no blocker findings
```

plus plan coverage satisfaction when applicable.

## Consequences

- Every shipped change has a human checkpoint
- CI on the target repo remains the final authority for production quality
- Session API exposes PR URL and review outcome for operator review
