# Documentation index

## Who owns what

One fact, one home. When they disagree, the owner wins:

| Fact | Owner |
|------|-------|
| What the project is, how to install and run it, API endpoint table | [../README.md](../README.md) |
| Invariants agents must obey: path/command safety, GX10 memory policy and per-lane tuning, merge-gate predicate, retry limits, model↔lane assignment, test and probe commands | [../AGENTS.md](../AGENTS.md) |
| Pipeline mechanics: planning-mode ladder, plan coverage gate, Tester skip rules, vector index, environment variables | [agent-orchestration.md](agent-orchestration.md) |
| Model candidates, probe results, rollback recipes | [model-evaluation.md](model-evaluation.md) |
| Planned backend work toward the interactive console: milestones, checkpoints, front-end change register | [roadmap-backend-interactive-console.md](roadmap-backend-interactive-console.md) |
| Why a decision was made | [adr/](adr/) |

Everything else links to the owner instead of restating it.

## Current

| Document | Audience | Contents |
|----------|----------|----------|
| [agent-orchestration.md](agent-orchestration.md) | Engineers | Pipeline flow, planning modes, coverage gates, vector index |
| [model-evaluation.md](model-evaluation.md) | ML ops | Live model matrix, probe legend, rollback notes |
| [roadmap-backend-interactive-console.md](roadmap-backend-interactive-console.md) | Engineers, UI repo devs | Phased backend plan for streaming, diff review, and codebase chat (M0–M5 landed; M6+ proposed) |
| [portfolio-blurb.md](portfolio-blurb.md) | Recruiters | Copy-paste project description |
| [adr/0010-manual-merge-gate.md](adr/0010-manual-merge-gate.md) | Architects | Why agents never auto-merge |
| [adr/0012-plan-code-modes-and-clarify.md](adr/0012-plan-code-modes-and-clarify.md) | Architects | Plan/Code user modes, clarify-before-run (backend MVP live; UI proposed) |
| [adr/0013-interpreter-clarify.md](adr/0013-interpreter-clarify.md) | Architects | LLM clarify with recommendations, Interpreter as the only multimodal role (Phase 1 live; attachments not built) |
| [adr/0014-run-queue-and-cancel.md](adr/0014-run-queue-and-cancel.md) | Architects | One run at a time, non-blocking start, cooperative cancel with hard escalation |
| [contracts/README.md](contracts/README.md) | UI repo devs | How to consume the console contracts |
| [contracts/chat-console-api.md](contracts/chat-console-api.md) | UI repo devs | Console API contract: state machine, endpoints, examples (Implemented — MVP) |
| [contracts/chat-console.openapi.yaml](contracts/chat-console.openapi.yaml) | UI repo devs | OpenAPI 3.1 spec for the console API (Implemented — MVP) |
| [../AGENTS.md](../AGENTS.md) | Sprint agents | Policies, lane tuning, test and probe commands |

## Archive

| Document | Contents |
|----------|----------|
| [archive/HISTORY.md](archive/HISTORY.md) | Bullets: Target console (former vision/roadmap/ADR 0011), retired test tiers, past models, pre-2026-07-19 benchmark runs |

## Examples

- [Session timeline JSON](examples/session-timeline.json) — anonymized sprint session event stream
