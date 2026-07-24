# Archived history (bullets only)

Past decisions, retired docs, and runs older than five days. For current behavior see [../README.md](../README.md), [../agent-orchestration.md](../agent-orchestration.md), and [../../AGENTS.md](../../AGENTS.md).

## Target console (collapsed from vision / roadmap / ADR 0011)

- Intended UX: Cursor-like browser chat console in a **separate repo** on a **non-GX host**; browser talks only to this FastAPI API, never to vLLM lanes `:8001`/`:8002`.
- User modes (see live [ADR 0012](../adr/0012-plan-code-modes-and-clarify.md)): **Plan** (no ship) vs **Code** (ship-to-PR → `awaiting_human`); clarify options → explicit confirm before any run.
- Phase 0 — docs PR: vision, roadmap, ADR 0011/0012, Current vs Target index.
- Phase 1 — delivered: console OpenAPI + markdown contracts + Pydantic stubs.
- Phase 1.5 — delivered: `/v1/console/*` MVP (in-memory store, deterministic clarify stub; Code mode reuses `/sprint/from-prompt`).
- Phase 2 (Proposed) — web console skeleton in separate repo: mode selector, clarify UI, confirm, session progress.
- Phase 3 (Proposed) — language-specialized coder lanes (one lane loaded at a time on GX10); ADR TBD.
- ADR 0011 (Proposed, archived): web console off GX10 so GPU memory stays for inference; API is the sole UI contract boundary.

## Past test strategy

- Retired multi-tier live pyramid (sandbox integration → GPU agent_live → vector A/B tiers).
- Deleted `docs/integration-testing.md` when live tiers were collapsed to mock unit tests + one opt-in 3-story trap e2e gate.
- Superseded `benchmarks/baseline.json` (2026-07-14) pointed at removed `tests/integration_live/...` and `./scripts/run_gx10_test_suite.sh`.

## Past model candidates (trimmed from live matrix)

- Work: `RedHatAI/Qwen3.6-35B-A3B-NVFP4` — prior candidate (tokenizer patch + Mamba flag); replaced by Qwen3-30B-A3B-Thinking-2507 NVFP4.
- Work rollback baseline kept in live doc: `Qwen/Qwen3-14B`.
- Coder rollback paths kept in live doc: `gdubicki/Qwen3-Coder-Next-NVFP4-GB10`, `Qwen/Qwen3-Coder-Next-FP8`.

## Benchmark runs (≤2026-07-18)

- **trap 2026-07-09** (3 runs): `stdlib_shadow_vector_repo` collection probes — `collect_exit_code=2`, expected `collection_error`.
- **capability 2026-07-10**: `story1_queue` → `awaiting_human` / `tool_loop` (~28m); `story2_retry` → `awaiting_human` / `static` (~25m); `story3_notify_clean` → `awaiting_human` / `tool_loop` (~34m).
- **from_prompt_integration 2026-07-10**:
  - `093746Z` — failed (`SCRUM-510`): Reviewer JSON decode empty response.
  - `134701Z` — failed (`SCRUM-518`): plan coverage / out-of-scope hits.
  - `144608Z` / `173126Z` — backlog completed but semantic index missed `ferry.py`.
  - `222209Z` — failed (`SCRUM-524`): Coder request timeout.
- **from_prompt_integration 2026-07-11** (`015206Z`): failed (`SCRUM-526`): Coder timeout / tests gate.
- JSON artifacts for the above were deleted after this summary (cutoff before 2026-07-19).
