# Roadmap: interactive sprint console

> Phase 0 was the documentation PR. Phase 1 (contract stubs) and Phase 1.5 (live console routes) are delivered. Phases 2–3 are **Proposed** — not scheduled, not implemented.

## Phase 0 — Documentation (this PR)

- [Product vision](vision/product-vision.md) exists and is marked Target — not implemented
- This roadmap exists with acceptance criteria per phase
- [ADR 0011](adr/0011-web-console-off-gx.md) and [ADR 0012](adr/0012-plan-code-modes-and-clarify.md) exist with Status: Proposed
- [docs/README.md](README.md) separates Current docs from Target/Proposed docs
- No changes to `src/`, `infra/`, `scripts/`, `tests/`, or `AGENTS.md`

## Phase 1 — API contract stubs (delivered)

- [x] Clarify contract documented: prompt in → suggested options out, accepting either a selected option or a user-custom answer — [contracts/chat-console-api.md](contracts/chat-console-api.md), [contracts/chat-console.openapi.yaml](contracts/chat-console.openapi.yaml)
- [x] Confirm contract documented: no sprint run starts without an explicit confirmation step — [confirm/start endpoints](contracts/chat-console-api.md#post-v1consolesessionsidconfirm)
- [x] Mode parameter (Plan / Code) documented for run-triggering endpoints — [session creation and start](contracts/chat-console-api.md#endpoints)
- [x] Pydantic schema stubs land behind the existing strict-schema conventions (`extra="forbid"`); no behavior change to current endpoints — `src/sprint_crew/schemas/console.py` (unit-tested; wired into routes in Phase 1.5)

## Phase 1.5 — Console routes (delivered)

- [x] `/v1/console/*` routes live in `src/sprint_crew/api/console.py` per [contracts/chat-console-api.md](contracts/chat-console-api.md): create/get session, messages, clarify, confirm, start, cancel
- [x] MVP scope: in-memory session store (single API worker), deterministic clarify stub (no LLM call), plan mode completes with a heuristic `plan_result`, code mode reuses the `/sprint/from-prompt` orchestration and exposes progress via `sprint_ref`
- [x] Existing `/sprint/*` and `/health` endpoints unchanged; no web UI in this repo (UI is the separate Phase 2 repo)

## Phase 2 — Web console skeleton (Proposed, separate repo)

- Console repo created on a non-GX host; browser traffic reaches only the FastAPI API, never vLLM lanes (:8001/:8002)
- Mode selector: Plan (no ship) vs Code (ship-to-PR path)
- Clarify-before-run UI: suggested options rendered as choices, plus a free-form custom answer field
- Explicit confirm action required before any sprint run starts
- Session progress view backed by the existing session/event timeline

## Phase 3 — Language-specialized coder lanes (Proposed)

- Multiple coder lane profiles specialized per language/stack, selected per ticket
- Lane selection respects the one-lane-at-a-time memory constraint on GX10
- Evaluation matrix comparing specialized lanes against the current single Coder lane
- ADR to follow when this phase is picked up (none yet, by decision)
