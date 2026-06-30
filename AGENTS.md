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

- Forbidden segments: `.git`, `.venv`, `node_modules`, caches, `.crew-index`.
- Placeholder paths (`<name>`, `<ext>`) are rejected.

## 4. Architecture (v2)

- **Single pipeline:** LangGraph `SprintPipeline` + one orchestrator (no dual CrewAI flows).
- **Inference:** three vLLM lanes — Coder :8001, Planner :8002, Judge :8003.
- **Lane lifecycle:** do not keep all three loaded 24/7 on 128 GB unified memory; start lanes on demand.

## 5. Orchestration

### 5.1 Side effects

Git clone, branch, commit, push, Jira transitions, and GitHub PR creation are **Python orchestrator** responsibilities only.

### 5.2 Entry points

- `POST /sprint/from-prompt` — user prompt → backlog plan → sprint cycle.
- `GET /sprint/session/{id}` — status and event timeline.
- Manual merge gate: human approves PR after `awaiting_human`.

## 6. Pydantic strict schemas

- All agent contracts use Pydantic v2 with `extra="forbid"`.
- No markdown fences in structured outputs — parse fail triggers Formatter (Coder) or retry.

## 7. Testing layers

| Marker | Purpose |
|--------|---------|
| `preflight` | Live vLLM probes A–D (`PREFLIGHT_LIVE=1`) |
| `live_agent` | Single agent, mocked IO |
| `live_e2e` | Full graph, mocked Jira/GitHub, real vLLM |

Preflight scripts: `scripts/probe_vllm_tools.py` (A+B), `scripts/probe_json.py` (C/C').

## 8. Agent roles

### 8.1 Merge gate

Review is **accepted** iff:

```python
review.passed and review.tests_passed and no finding.severity == "blocker"
```

Implemented in `sprint_crew.orchestrator.merge_gate.review_accepted`.

#### 8.1.1 Severity ladder

- **blocker** — must fix before merge (correctness, security, failing tests).
- **warning** — should fix; does not block alone.
- **nit** — style only.

### 8.2 Retry

- Max review retries: `MAX_REVIEW_RETRIES=4` (orchestrator, not LangGraph internal).
- Work tree preserved between retries.
- `prior_review_feedback` injected into Coder and TechLead prompts.

### 8.3 Manual merge

Human merges PR after `awaiting_human` — agents never auto-merge to main (ADR 0010).

### 8.4 Producer vs reporter (vLLM)

| Type | Agents | Inference |
|------|--------|-------------|
| **Producer** | Coder, Tester | Tool loop on Coder lane; **no** `response_format` mid-loop |
| **Reporter** | TechLead, Formatter, Reviewer, ScrumMaster | `output_type` / vLLM guided JSON on Planner or Judge lane; no mutating tools (Reviewer: read-only OK) |

- **Coder:** `qwen3-coder-30b`, T=0, max_turns=12, tools ON.
- **Planner:** `qwen3-14b`, TaskPlan JSON (preflight C).
- **Judge:** `gemma-4-12b`, ReviewOutcome JSON (preflight C').
