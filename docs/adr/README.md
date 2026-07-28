# Architecture decision records

| ADR | Status | Decision |
|---|---|---|
| [0010](0010-manual-merge-gate.md) | Accepted | Merge gate stays manual — no auto-merge on green review |
| [0011](0011-web-console-off-gx.md) | Archived | Web console off GX10; the API is the sole UI contract boundary |
| [0012](0012-plan-code-modes-and-clarify.md) | Accepted | Plan vs Code console modes, clarify-then-confirm before any run |
| [0013](0013-interpreter-clarify.md) | Accepted | Model-generated clarify via the Interpreter on the Work lane |
| [0014](0014-run-queue-and-cancel.md) | Accepted | Single-slot run queue with cooperative cancel and a hard-cancel watchdog |
| [0015](0015-human-review-gate.md) | Accepted | A run parks on per-file human review before shipping; reject is feedback, never a partial commit |
| [0016](0016-durable-repo-index.md) | Accepted | A durable per-repo vector index with a per-run overlay for uncommitted work |
| [0017](0017-codebase-chat.md) | Accepted | Read-only codebase chat streaming on the session timeline; holds the run slot, refused during a run, no warm lane |

**Why the numbering starts at 0010.** It does not continue an earlier series — 0001–0009 were
never written. The practice began when the console work started, and the first record was
numbered 0010 to sit alongside the milestone numbering used in
[roadmap-backend-interactive-console.md](../roadmap-backend-interactive-console.md).
