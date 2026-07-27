# AGENTS.md — sprint-crew-v2 policies

Policies for autonomous sprint agents on GX10 (Pydantic AI + LangGraph + vLLM).

This file owns the **invariants**: safety rules, GX10 memory policy, the merge-gate
predicate, retry limits, model↔lane assignment, and test commands. Pipeline *mechanics*
live in [`docs/agent-orchestration.md`](docs/agent-orchestration.md); the ownership map is
in [`docs/README.md`](docs/README.md).

## 1. Commit and workspace discipline

- One ticket → one branch → one PR. No drive-by refactors.
- Commit messages reference ticket key (e.g. `DEMO-1: add greeter hello()`).
- Never commit secrets, `.env`, or credentials.

## 2. Security

- All file paths go through `resolve_safe_path` — no `..`, no `.git`, no `.venv`.
- `run_command` uses an allowlist only; `npm` always gets `--ignore-scripts`.

## 3. Path and command safety

- Forbidden segments: `.git`, `.venv`, `node_modules`, caches.
- Placeholder paths (`<name>`, `<ext>`) are rejected.

## 4. Architecture (v2)

- **Single pipeline:** LangGraph `build_sprint_graph` / `run_sprint_cycle` is the only sprint-cycle engine (from-ticket and backlog). [`batch_cycle.py`](src/sprint_crew/orchestrator/batch_cycle.py) is a thin Jira/workspace orchestrator that calls `create_and_run_cycle` per ticket (no dual CrewAI flows).
- **Inference:** two vLLM lanes — Coder :8001, Work :8002 (Work hosts Interpreter clarify, ScrumMaster prep, TechLead, Formatter, and Reviewer; the separate Nemotron prep lane was merged into Work).
- **Multimodal boundary:** the Work model (Qwen3.6) accepts images, but **only the Interpreter may send them** ([ADR 0013](docs/adr/0013-interpreter-clarify.md)). Every other role is text-only and consumes the Interpreter's derived text. Do not add image content parts to ScrumMaster, TechLead, Coder, Tester, or Reviewer prompts.
- **Lane lifecycle:** do not keep all lanes loaded 24/7 on 128 GB unified memory; start lanes on demand.

### 4.1 GX10 unified memory (128 GB)

- Never keep 2+ vLLM lanes with weights loaded simultaneously in production/dev.
- **from-prompt API:** ScrumMaster prep runs on Work lane in [`src/sprint_crew/api/app.py`](src/sprint_crew/api/app.py) before backlog orchestration (BacklogPlan → Jira tickets).
- **Per-ticket LangGraph cycle:** `techLeadPlan` → `codeImplement` → `testImplement` (when required) → `review` → merge gate → ship; lane swaps: Work → Coder → Work within each cycle.
- `smoke_cycle` / API: do **not** call `lane-ctl start all` before a cycle (optional `--preflight-lanes` starts coder only).
- Target `gpu_memory_utilization` per lane (one lane loaded at a time) — **per-lane tuning** on GX10/GB10 ([`infra/models.yaml`](infra/models.yaml)), NVFP4 weights: coding **0.85** (~46 GB weights, 131k+ context), work **0.65** (~23 GB weights incl. vision tower, 131k context). NVFP4 MoE on GB10 (sm121) needs Marlin GEMM env flags (`VLLM_NVFP4_GEMM_BACKEND=marlin`) — see [`infra/docker-compose.yml`](infra/docker-compose.yml); 0.90+ still risks wedging the host ([vLLM #46307](https://github.com/vllm-project/vllm/issues/46307), [vllm-gb10](https://github.com/shamily/vllm-gb10)). Do not sum utilization across containers.
- Backlog runs: `run_backlog_batched` creates Jira tickets and sequentially calls `create_and_run_cycle` (same LangGraph as from-ticket). `backlog_run_id` skips stopping the Work lane after review within a cycle; `_stop_all_lanes` runs in `finally`.
- **Model serving split:** vLLM container flags and HF model IDs live in [`infra/docker-compose.yml`](infra/docker-compose.yml); [`infra/models.yaml`](infra/models.yaml) is the Python client config (ports, `served_name`). Its `tool_call_parser` fields are documentation-only — the parser vLLM actually uses comes from the compose flags.
- After a failed lane test, always `./scripts/lane-ctl.sh stop all` before retrying.
- If a lane is slow despite health=OK, check whether another vLLM container is still running (`docker ps`, `nvidia-smi`).

## 5. Orchestration

### 5.1 Side effects

Git clone, branch, commit, push, Jira transitions, and GitHub PR creation are **Python orchestrator** responsibilities only.

### 5.2 Entry points

Endpoint table: [README](README.md#api). Policy: `/sprint/from-prompt` runs ScrumMaster
first, `/sprint/from-ticket` skips it, and `approve` only **records** human approval — no
endpoint merges to main.

### 5.3 Acceptance criteria vs test commands

- Jira `acceptance_criteria` is **human prose** (bullets, behavior) — never executed as shell.
- TechLead interprets AC + repo context → `TaskPlan.acceptance_tests` (allowlisted commands only).
- `assess_prompt_complexity` gates backlog normalization (story merge/cap) and vector indexing — **not** lane routing.
- Reviewer receives original ticket AC for behavioral/scope review; `tests_passed` comes from orchestrator exit codes, never from the model's claim.

### 5.4 Multi-file orchestration

Mechanics — planning-mode ladder, coverage gate, Tester skip rules, env vars — are in
[`docs/agent-orchestration.md`](docs/agent-orchestration.md). The rules that bind you:

- **Never bypass the plan.** All tickets go through `techLeadPlan`; do not add a graph branch that skips it.
- **Plan coverage is deterministic and blocking** — Python compares plan paths to the git diff. You cannot argue a file into coverage; touch it or replan.
- **Tester writes only under `tests/`.** Paths outside it soft-fail back to the model.
- **write_file:** capped at `MAX_WRITE_FILE_BYTES`; use `apply_patch` for large edits.

## 6. Pydantic strict schemas

- All agent contracts use Pydantic v2 with `extra="forbid"`.
- No markdown fences in structured outputs — parse fail triggers Formatter (Coder) or retry.

## 7. Testing

Automated coverage is the mock-only unit suite plus one opt-in end-to-end gate;
other real-cycle verification is manual, on demand.

| Command | What it tests |
|---------|---------------|
| `pytest tests/unit -q` | Logic, tools, routing, agent unit tests (`tests/unit/agents/`) |
| `VECTOR_AGENT_LIVE=1 VLLM_LIVE=1 pytest tests/agent_live/trap/test_from_prompt_3story_trap.py -v` | 3-story from-prompt e2e vs adversarial stdlib-shadow fixture (GX10 lanes; the test starts the vector stack itself) |
| `scripts/smoke_cycle.py` | Manual full LangGraph cycle on `fixtures/repo` (real lanes) |
| `scripts/verify_integrations.py` | Jira/GitHub credential smoke |
| `scripts/benchmark_pipeline.py` | Scenario matrix → JSON metrics |

Manual vLLM probes: `scripts/probe_vllm_tools.py` (tool_calls on work/coder lanes),
`scripts/probe_json.py` (structured JSON), `scripts/probe_interpreter.py` (clarify quality
and, with `--image`, vision), `scripts/probe_vector_index.py` (Qdrant round-trip).
Probe A–D legend for the model matrix: [docs/model-evaluation.md](docs/model-evaluation.md).

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
| **Explorer** | TechLead (COMPLEX only) | Read-only tool loop on Work lane (:8002, `qwen3_coder` parser), then structured TaskPlan JSON |
| **Prep** | ScrumMaster | `BacklogPlan` JSON on Work lane (:8002), loaded only for `from-prompt` |

- **Coder:** `laguna-s-2.1-nvfp4` (Poolside Laguna S 2.1 NVFP4, GB10; vLLM ≥0.25.1 `poolside_v1`), Poolside eval-certified sampling **T=0.7 / top_p=0.95 / top_k=20** (raw defaults degrade NVFP4 output — pinned in `config.py` per request *and* via `--override-generation-config`), `MAX_CODER_TURNS=32`, tools ON, `max-model-len=131072`, `gpu-memory-utilization=0.85`. Thinking is OFF for early attempts and escalates per-request from attempt 2 (`--reasoning-parser poolside_v1` loaded so the trace is stripped before tool parsing; longer timeout absorbs the 118B MoE reasoning trace that otherwise overruns the request deadline — see `infra/docker-compose.yml` and §8.2 Reasoning escalation). Early exit when acceptance tests pass, diff is non-empty, and plan coverage is satisfied (`CODER_EARLY_EXIT_REQUIRES_COVERAGE`). Mid-loop model errors (context overflow, request timeout) hand off partial work rather than failing the cycle. Rollback: `gdubicki/Qwen3-Coder-Next-NVFP4-GB10`.
- **Work:** `qwen3.6-35b-a3b-nvfp4` ([RedHatAI/Qwen3.6-35B-A3B-NVFP4](https://huggingface.co/RedHatAI/Qwen3.6-35B-A3B-NVFP4), `qwen3_5_moe`, ~22 GB incl. vision tower, ~3B active, `gpu-memory-utilization=0.65`), TaskPlan / CodeChange / ReviewOutcome / BacklogPlan JSON; TechLead tool_loop for COMPLEX tickets (`MAX_TECHLEAD_TURNS`, `--tool-call-parser qwen3_coder --reasoning-parser qwen3` on :8002). Also runs ScrumMaster prep decomposition (merged lane). Multimodal — only the Interpreter may send images (§3, [ADR 0013](docs/adr/0013-interpreter-clarify.md)). No quant patch needed: this ckpt declares `quant_method: compressed-tensors` itself. Rollback: `qwen3-30b-a3b-thinking` (Qwen3-30B-A3B-Thinking-2507 NVFP4, `qwen3_moe`, text-only, `gpu-memory-utilization=0.50`) — that ckpt omits `quant_method`, so it needs `scripts/patch_work_quant.py` run once after `hf download`, otherwise vLLM loads it unquantized and dies on `w2_weight_scale_2`. See `infra/models.yaml`.
- **File context:** orchestrator gathers `git diff` after Coder/Tester and injects into Formatter + Reviewer prompts (`workspace_diff` in graph state). Plan coverage also reads `git status --porcelain` for untracked files. TechLead receives `enrich_repo_context` (manifest, pre_search, grep, semantic hits). `validate_plan_paths_exist` rejects phantom paths before Coder; `snapshot_baseline_paths` at session start feeds coverage phantom detection.

### 8.5 Vector index (Qdrant)

- **Default on** (`VECTOR_INDEX_ENABLED=true`); **TRIVIAL** tickets skip indexing and retrieval.
- Retrieval never overrides ground truth: merge gate and plan coverage stay deterministic (git diff + `snapshot_baseline_paths`).
- Dev stack: `./scripts/lane-ctl.sh start vector` (Qdrant :6333 + embed sidecar :8080).

Indexing triggers, agent-by-agent integration, and tuning env vars: [`docs/agent-orchestration.md`](docs/agent-orchestration.md#vector-index-qdrant--embeddings).

### 8.6 Interpreter (clarify)

Runs on the Work lane before any planning, for `/v1/console/*` sessions only ([ADR 0013](docs/adr/0013-interpreter-clarify.md)).

- **Structured output only** — no tools, no repo writes, no side effects.
- **Zero questions is a valid answer.** A clear request must go straight to `ready`; do not add a floor on question count.
- **Every question carries a recommendation** (`recommended_suggestion_id` + per-option `rationale`). A user who accepts only the recommendations must get sensible work.
- **Python owns identifiers.** The model returns ordered questions and a `recommended_index`; ids are assigned and the index clamped in `agents/interpreter.py`. Never trust model-generated ids.
- **Clarify degrades, never blocks.** Cold lane or a failed call falls back to the deterministic questions in `api/console/clarify.py`; `CLARIFY_AUTOSTART_LANE=true` opts into waiting for a lane load instead.
- Interpreter maps to `Role.WORK`. Adding a dedicated `Role` for it is safe: `ensure_lane` stops other lanes by lane name, not by `Role`, so two roles sharing the work lane will not stop the container they are about to start.
