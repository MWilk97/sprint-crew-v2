# AGENTS.md — sprint-crew-v2 policies

Policies for autonomous sprint agents on GX10 (Pydantic AI + LangGraph + vLLM).

## 1. Commit and workspace discipline

- One ticket → one branch → one PR. No drive-by refactors.
- Commit messages reference ticket key (e.g. `DEMO-1: add greeter hello()`).
- Never commit secrets, `.env`, or credentials.

## 2. Security

- All file paths go through `resolve_safe_path` — no `..`, no `.git`, no `.venv`.
- `run_command` uses an allowlist only; `npm` always gets `--ignore-scripts`.
- Side effects (Jira, GitHub, git push) run in the **orchestrator**, never inside LLM tools.

## 3. Path and command safety

- Forbidden segments: `.git`, `.venv`, `node_modules`, caches.
- Placeholder paths (`<name>`, `<ext>`) are rejected.

## 4. Architecture (v2)

- **Single pipeline:** LangGraph `build_sprint_graph` / `run_sprint_cycle` is the only sprint-cycle engine (from-ticket and backlog). [`batch_cycle.py`](src/sprint_crew/orchestrator/batch_cycle.py) is a thin Jira/workspace orchestrator that calls `create_and_run_cycle` per ticket (no dual CrewAI flows).
- **Inference:** two vLLM lanes — Coder :8001, Work :8002 (Work hosts ScrumMaster prep, TechLead, Formatter, and Reviewer; the separate Nemotron prep lane was merged into Work).
- **Lane lifecycle:** do not keep all lanes loaded 24/7 on 128 GB unified memory; start lanes on demand.

### 4.1 GX10 unified memory (128 GB)

- Never keep 2+ vLLM lanes with weights loaded simultaneously in production/dev.
- **from-prompt API:** ScrumMaster prep runs on Work lane in [`src/sprint_crew/api/app.py`](src/sprint_crew/api/app.py) before backlog orchestration (BacklogPlan → Jira tickets).
- **Per-ticket LangGraph cycle:** `techLeadPlan` → `codeImplement` → `testImplement` (when required) → `review` → merge gate → ship; lane swaps: Work → Coder → Work within each cycle.
- `smoke_cycle` / API: do **not** call `lane-ctl start all` before a cycle (optional `--preflight-lanes` starts coder only).
- Target `gpu_memory_utilization` per lane (one lane loaded at a time) — **per-lane tuning** on GX10/GB10 ([`infra/models.yaml`](infra/models.yaml)), NVFP4 weights: coding **0.85** (~46 GB weights, 131k+ context), work **0.50** (~20 GB weights, 131k context). NVFP4 MoE on GB10 (sm121) needs Marlin GEMM env flags (`VLLM_NVFP4_GEMM_BACKEND=marlin`) — see [`infra/docker-compose.yml`](infra/docker-compose.yml); 0.90+ still risks wedging the host ([vLLM #46307](https://github.com/vllm-project/vllm/issues/46307), [vllm-gb10](https://github.com/shamily/vllm-gb10)). Do not sum utilization across containers.
- Backlog runs: `run_backlog_batched` creates Jira tickets and sequentially calls `create_and_run_cycle` (same LangGraph as from-ticket). `backlog_run_id` skips stopping the Work lane after review within a cycle; `_stop_all_lanes` runs in `finally`.
- After a failed lane test, always `./scripts/lane-ctl.sh stop all` before retrying.
- If a lane is slow despite health=OK, check whether another vLLM container is still running (`docker ps`, `nvidia-smi`).

## 5. Orchestration

### 5.1 Side effects

Git clone, branch, commit, push, Jira transitions, and GitHub PR creation are **Python orchestrator** responsibilities only.

### 5.2 Entry points

- `POST /sprint/from-prompt` — user prompt → BacklogPlan → Jira tickets → sequential sprint cycles (`BacklogRun`).
- `GET /sprint/backlog/{run_id}` — backlog orchestration status and session IDs.
- `POST /sprint/from-ticket` — existing Jira ticket → sprint cycle (skips ScrumMaster).
- `GET /sprint/session/{id}` — status and event timeline.
- `POST /sprint/session/{id}/approve` — record human approval (no auto-merge).
- Manual merge gate: human approves PR after `awaiting_human`.

### 5.3 Acceptance criteria vs test commands

- Jira `acceptance_criteria` is **human prose** (bullets, behavior) — never executed as shell.
- TechLead interprets AC + repo context → `TaskPlan.acceptance_tests` (allowlisted commands only).
- Every sprint cycle runs **TechLead → Coder+Formatter → Tester (conditional) → Review**. All tickets use **TechLead** via `techLeadPlan`; planning mode is selected internally (`template`, `static`, `tool_loop`, `template_fallback`).
- Ticket complexity uses `assess_ticket_complexity` (COMPLEX → tool_loop; TRIVIAL/SIMPLE → template fast-path then static). `assess_prompt_complexity` gates backlog normalization (story merge/cap) and vector indexing — not lane routing.
- Reviewer receives original ticket AC for behavioral/scope review; `tests_passed` from orchestrator exit codes.

### 5.4 Multi-file orchestration

See [`docs/agent-orchestration.md`](docs/agent-orchestration.md) for the full flow.

- **TechLead:** always via `techLeadPlan`; ladder is template fast-path (TRIVIAL/SIMPLE) → static LLM snapshot → tool_loop (COMPLEX only) → template_fallback after validation retries.
- **Coder:** step orchestration when `CODER_STEP_MODE=true` and multiple plan steps; fresh session per step.
- **Plan coverage:** Python compares `files_to_touch` / `step.files` to git diff + untracked paths; gates early exit and triggers continuation rounds (`MAX_COVERAGE_ROUNDS`).
- **Tester:** skipped when orchestrator-verified acceptance tests are green, or when the plan assigns test files to Coder; invoked when AC is red and source changed without `tests/` diff (unless plan requires tests in Coder steps).
- **write_file:** capped at `MAX_WRITE_FILE_BYTES`; use `apply_patch` for large edits.

## 6. Pydantic strict schemas

- All agent contracts use Pydantic v2 with `extra="forbid"`.
- No markdown fences in structured outputs — parse fail triggers Formatter (Coder) or retry.

## 7. Testing layers

| Marker | Purpose |
|--------|---------|
| `preflight` | Live vLLM probes A–C via pytest; probe D = `scripts/smoke_cycle.py --coder-only` (manual) |
| `agent_live` | Single-agent tests on fixture repo (`VLLM_LIVE=1` for GPU agents) |
| `integration_live` | Real sandbox Jira + GitHub (`INTEGRATION_LIVE=1`) |
| `vllm_live` | Real vLLM lanes (`VLLM_LIVE=1`) |

| Command | What it tests |
|---------|---------------|
| `pytest tests/unit -q` | Logic, tools, routing, agent unit tests (`tests/unit/agents/`) |
| `INTEGRATION_LIVE=1 pytest tests/integration_live -m "integration_live and not vllm_live" -q` | Sandbox Jira/GitHub + API routes (no GPU) |
| `./scripts/run_gx10_test_suite.sh` | Work lane preflight → coder block (preflight + greeter ship_live); lane timeout **1200 s** |
| `./scripts/run_gx10_test_suite.sh --with-agent-live` | Adds diagnostic tech_lead static plan test (non-greeter; ship_live covers full pipeline) |
| `./scripts/run_gx10_test_suite.sh --with-email` | Same coder block + email ship_live |
| `PREFLIGHT_LIVE=1 pytest -m preflight` | vLLM probe scripts via pytest (work lane tools + JSON + backlog) |
| `scripts/smoke_cycle.py` | Manual reference baseline |
| `scripts/verify_integrations.py` | Jira/GitHub credential smoke |
| `scripts/benchmark_pipeline.py` | Scenario matrix → JSON metrics |

See [`docs/integration-testing.md`](docs/integration-testing.md) for sandbox setup and cleanup.

Preflight scripts: `scripts/probe_vllm_tools.py` (A+B+C on work/coder lanes), `scripts/probe_json.py` (structured JSON on work lane), `test_probe_backlog_plan` (BacklogPlan JSON). Probe D: `scripts/smoke_cycle.py --coder-only`.

## 8. Agent roles

### 8.1 Merge gate

Review is **accepted** iff:

```python
review.passed and review.tests_passed and no finding.severity == "blocker" and plan_coverage.satisfied
```

Implemented in `sprint_crew.orchestrator.merge_gate.review_accepted`.

#### 8.1.1 Severity ladder

- **blocker** — must fix before merge (correctness, security, failing tests).
- **warning** — should fix; does not block alone.
- **nit** — style only.

### 8.2 Retry

- Max review retries: `MAX_REVIEW_RETRIES=4` (orchestrator, not LangGraph internal).
- Max plan retries: `MAX_PLAN_RETRIES=2` (gives TechLead a second replan on `retry_scope=plan`).
- Work tree preserved between retries.
- `prior_review_feedback` injected into Coder and TechLead prompts. A source-build failure whose source package name shadows a Python stdlib module (e.g. `src/platform/`) surfaces an explicit `STDLIB SHADOW DETECTED` hint (`retry._stdlib_shadow_packages`).
- Smart routing: `retry_scope=code` → `codeImplement` only; `retry_scope=plan` → `techLeadPlan` (Reviewer sets scope; Python keyword fallback in `resolve_retry_scope`).
- **Reasoning escalation:** Coder attempts `0..N` run thinking-OFF; from `CODER_THINKING_ESCALATION_ATTEMPT=2` onward the Coder enables per-request `enable_thinking` and swaps to `CODER_THINKING_TIMEOUT_S=1800` (vs `CODER_REQUEST_TIMEOUT_S=600`). Cheap deterministic-ish attempts first, expensive reasoning only when they stall.

### 8.3 Manual merge

Human merges PR after `awaiting_human` — agents never auto-merge to main (ADR 0010).

### 8.4 Producer vs reporter (vLLM)

| Type | Agents | Inference |
|------|--------|-------------|
| **Producer** | Coder, Tester | Tool loop on Coder lane; **no** `response_format` mid-loop |
| **Reporter** | TechLead (structured JSON), Formatter, Reviewer, Tester reporter | `output_type` / vLLM guided JSON on Work lane (:8002) |
| **Explorer** | TechLead (COMPLEX only) | Read-only tool loop on Work lane (:8002, `hermes` parser), then structured TaskPlan JSON |
| **Prep** | ScrumMaster | `BacklogPlan` JSON on Work lane (:8002), loaded only for `from-prompt` |

- **Coder:** `laguna-s-2.1-nvfp4` (Poolside Laguna S 2.1 NVFP4, GB10; vLLM ≥0.25.1 `poolside_v1`), Poolside eval-certified sampling **T=0.7 / top_p=0.95 / top_k=20** (raw defaults degrade NVFP4 output — pinned in `config.py` per request *and* via `--override-generation-config`), `MAX_CODER_TURNS=32`, tools ON, `max-model-len=131072`, `gpu-memory-utilization=0.85`. Thinking is OFF for early attempts and escalates per-request from attempt 2 (`--reasoning-parser poolside_v1` loaded so the trace is stripped before tool parsing; longer timeout absorbs the 118B MoE reasoning trace that otherwise overruns the request deadline — see `infra/docker-compose.yml` and §8.2 Reasoning escalation). Early exit when acceptance tests pass, diff is non-empty, and plan coverage is satisfied (`CODER_EARLY_EXIT_REQUIRES_COVERAGE`). Mid-loop model errors (context overflow, request timeout) hand off partial work rather than failing the cycle. Rollback: `gdubicki/Qwen3-Coder-Next-NVFP4-GB10`.
- **Work:** `qwen3-30b-a3b-thinking` (Qwen3-30B-A3B-Thinking-2507 NVFP4, `qwen3_moe`, text-only, ~3B active), TaskPlan / CodeChange / ReviewOutcome / BacklogPlan JSON; TechLead tool_loop for COMPLEX tickets (`MAX_TECHLEAD_TURNS`, `--tool-call-parser hermes --reasoning-parser qwen3` on :8002). Also runs ScrumMaster prep decomposition (merged lane). Plain `qwen3_moe` (full attention, `Qwen2Tokenizer`): no tokenizer patch and no `--max-num-batched-tokens` Mamba workaround (both were only needed by the prior Qwen3.6 multimodal ckpt). Setup: the ModelOpt NVFP4 ckpt omits `quant_method` in `config.json`, so after `hf download` run `scripts/patch_work_quant.py` once (adds `quant_method: modelopt` so vLLM 26.04 selects the `modelopt_fp4` NVFP4 Marlin backend; otherwise it loads unquantized and dies on `w2_weight_scale_2`).
- **File context:** orchestrator gathers `git diff` after Coder/Tester and injects into Formatter + Reviewer prompts (`workspace_diff` in graph state). Plan coverage also reads `git status --porcelain` for untracked files. TechLead receives `enrich_repo_context` (manifest, pre_search, grep, semantic hits). `validate_plan_paths_exist` rejects phantom paths before Coder; `snapshot_baseline_paths` at session start feeds coverage phantom detection.

### 8.5 Vector index (Qdrant)

- **Default on** (`VECTOR_INDEX_ENABLED=true`); **TRIVIAL** tickets skip indexing and retrieval.
- Dev stack: `./scripts/lane-ctl.sh start vector` (Qdrant :6333 + embed sidecar :8080).
- A/B comparison: `VECTOR_AGENT_LIVE=1 VLLM_LIVE=1 pytest -m vector_agent_live` (full cycle, not in default gx10 suite).
- **Capability** (per-story, SOFT in full suite): `VECTOR_AGENT_LIVE=1 VLLM_LIVE=1 pytest tests/agent_live/capability/ -m "vector_agent_live and agent_capability" -v`
- **Integration nightly** (2-story from-prompt, HARD gate): `VECTOR_AGENT_LIVE=1 VLLM_LIVE=1 pytest tests/agent_live/integration/ -m "vector_agent_live and agent_integration and nightly" -v` (~1–1.5h; requires `./scripts/lane-ctl.sh start vector`)
- **Trap** (adversarial, STRICT by default — a trap the agent falls for fails the test): `VECTOR_AGENT_LIVE=1 VLLM_LIVE=1 pytest tests/agent_live/trap/ -m "vector_agent_live and agent_trap" -v` (set `VECTOR_TRAP_SOFT=1` for report-only benchmark runs)
- Scorecard: `python scripts/agent_scorecard.py` (aggregates `benchmarks/results/*.json`)
- Vector stack without vLLM: `VECTOR_LIVE=1 pytest -m vector_live -q`
