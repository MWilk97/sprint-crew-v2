# Documentation index

## Current

| Document | Audience | Contents |
|----------|----------|----------|
| [agent-orchestration.md](agent-orchestration.md) | Engineers | Pipeline flow, planning modes, coverage gates, vector tiers |
| [model-evaluation.md](model-evaluation.md) | ML ops | Model comparison matrix, rollback notes |
| [architecture.md](architecture.md) | Visitors / CV | High-level system overview |
| [portfolio-blurb.md](portfolio-blurb.md) | Recruiters | Copy-paste project description |
| [adr/0010-manual-merge-gate.md](adr/0010-manual-merge-gate.md) | Architects | Why agents never auto-merge |
| [contracts/chat-console-api.md](contracts/chat-console-api.md) | UI repo devs | Console API contract: state machine, endpoints, examples (Implemented — MVP store + stub clarify) |
| [contracts/chat-console.openapi.yaml](contracts/chat-console.openapi.yaml) | UI repo devs | OpenAPI 3.1 spec for the console API (Implemented — MVP) |
| [adr/0012-plan-code-modes-and-clarify.md](adr/0012-plan-code-modes-and-clarify.md) | Architects | Plan/Code user modes, clarify-before-run (backend MVP live; UI proposed) |
| [../AGENTS.md](../AGENTS.md) | Sprint agents | Policies, lane tuning, test markers |

## Target / Proposed (not implemented)

| Document | Audience | Contents |
|----------|----------|----------|
| [vision/product-vision.md](vision/product-vision.md) | Everyone | Target UX: interactive sprint console (Target) |
| [roadmap.md](roadmap.md) | Engineers / PM | Phases 0–3 with acceptance criteria (Proposed) |
| [adr/0011-web-console-off-gx.md](adr/0011-web-console-off-gx.md) | Architects | Web console in separate repo, off GX10 (Proposed) |
| [contracts/README.md](contracts/README.md) | UI repo devs | How to consume the console contracts; Current vs Proposed |

## Examples

- [Session timeline JSON](examples/session-timeline.json) — anonymized sprint session event stream
