# Integration testing runbook

Live tests exercise real Jira and GitHub APIs. They are **opt-in** and never run in CI.

## Quick start (new machine)

```bash
cp .env.example .env          # fill Jira + GitHub tokens
chmod +x scripts/*.sh
./scripts/setup_integration_sandbox.sh
```

This script: creates `.venv` if missing, sets `USE_MOCK_INTEGRATIONS=false`, bootstraps the fixture repo, runs `verify_integrations.py`, and runs `integration_live` tests (no GPU).

**GX10 host bootstrap (once per machine):** install `git`, `docker.io`, `docker-compose-v2`, `python3.12-venv`, `nodejs`, `npm`, `curl`, `jq`, `build-essential`; enable Docker; add your user to the `docker` group; configure NVIDIA container runtime if `nvidia-ctk` is available.

On GX10 with vLLM:

```bash
./scripts/run_gx10_test_suite.sh              # work lane preflight + greeter ship_live (~25–40 min)
./scripts/run_gx10_test_suite.sh --with-email # also email ship_live in the same warm coder block
./scripts/run_gx10_test_suite.sh --with-agent-live  # per-agent tests + ship_live
```

GX10 lane orchestration (`run_gx10_test_suite.sh`):

1. **Phase 1:** Work lane preflight (tools + JSON) → optional **agent_live** (reviewer, tech_lead, formatter) → stop Work lane
2. **Phase 2:** `lane_hard_reset` → one coder cold start → optional **agent_live coder + tester** → coder preflight → **ship_live greeter** (real Jira/GitHub/vLLM, no mocks)
3. Optional email ship_live in the same coder block (`--with-email`)

Lane health waits **1200 s** (matches `lanes.ensure_lane`). On failure: docker logs + one retry after `lane_hard_reset`. `tests/helpers/ship_live_cycle.py` also calls `wait_lane_healthy` before the agent cycle.

Full A–Z (Tier 1+2+3):

```bash
./scripts/run_full_test_suite.sh
./scripts/run_full_test_suite.sh --skip-gpu   # unit + sandbox only (~2 min)
./scripts/run_full_test_suite.sh --with-email # GX10 includes email scenario
```

## Test tiers

| Tier | Profile | Command | Time |
|------|---------|---------|------|
| 1 | Dev/CI | `pytest tests/unit -q` | ~1 s |
| 2 | Sandbox | `INTEGRATION_LIVE=1 pytest tests/integration_live -m "integration_live and not vllm_live" -q` | ~1–2 min |
| 3 | GX10 GPU | `./scripts/run_gx10_test_suite.sh` | ~25–40 min |

Tier 1 covers orchestration logic, tools, routing, and **agent unit tests** under `tests/unit/agents/` (mock LLM only). Tier 2 uses **real Jira/GitHub** and real `SessionStore` for API routes. Tier 3: work lane preflight, then **one coder block** with greeter ship_live (mandatory smoke). Optional: `tests/agent_live/` (`--with-agent-live`), email ship_live (`--with-email`). Lane startup timeout: **20 min** with one automatic retry.

See [`docs/agent-orchestration.md`](agent-orchestration.md) for coverage gate and step-mode behavior.

Prerequisite check only:

```bash
python scripts/check_sandbox_prerequisites.py
python scripts/check_sandbox_prerequisites.py --gx10 --require-fixture
```

## Required `.env` variables (Tier 2+)

| Variable | Purpose |
|----------|---------|
| `USE_MOCK_INTEGRATIONS=false` | Required for live clients |
| `JIRA_URL`, `JIRA_EMAIL`, `JIRA_API_TOKEN` | Atlassian Cloud auth |
| `JIRA_PROJECT_KEY` | Sandbox project (e.g. `SCRUM`) |
| `JIRA_REVIEW_TRANSITION` | Workflow transition after ship (e.g. `W trakcie weryfikacji`) |
| `GITHUB_TOKEN` | PAT with `repo` scope |
| `GITHUB_REPO` | Sandbox repo `owner/name` |
| `GITHUB_FIXTURE_REPO_GREETER` | Fixture repo: `main` = [`fixtures/repo`](../fixtures/repo) |

Optional:

| Variable | Purpose |
|----------|---------|
| `JIRA_AC_FIELD` | Enables custom-field AC test in `test_jira_roundtrip.py` |
| `HF_TOKEN` | Required for Tier 3 (vLLM model download) |
| `MAX_PLAN_RETRIES` | Cap expensive replan retries (default `1`) |

Local paths (`SPRINT_WORKSPACE_BASE`, etc.) default to `$HOME/...` via `config.py` when omitted.

## Jira sandbox checklist

1. Create a dedicated Jira project (not production).
2. Ensure `JIRA_REVIEW_TRANSITION` matches an available workflow transition name exactly.
3. Test issues use summary prefix `[sprint-crew-test]` for manual cleanup.

## Fixture repo (ship_live_cycles)

`test_ship_live_cycles` clones a **dedicated** GitHub sandbox whose `main` branch matches [`fixtures/repo`](../fixtures/repo) (greeter + validators tests). Ship pushes to the same repo so PRs share history with `main`.

Bootstrap (creates repo via API if missing):

```bash
# Set GITHUB_FIXTURE_REPO_GREETER in .env
./scripts/bootstrap_fixture_repos.sh
```

Re-run bootstrap after changing files under `fixtures/repo`. Do not bootstrap during an active ship_live run (`.bootstrap.lock` prevents overlap).

## Vector e2e fixture

| Variant | Purpose |
|---------|---------|
| `fixtures/vector_repo` (base) | Stories 1–2 capability; story 3 **trap** (stdlib `platform` shadow) |
| `fixtures/vector_repo/overlays/story3_clean/` | Overlay applied at test time — story 3 REST without import trap |

Use `copy_vector_fixture(tmp_path, overlay="story3_clean")` from [`tests/helpers/vector_fixtures.py`](../tests/helpers/vector_fixtures.py).

A ~25-file mini platform with an outbound **message ferry** dispatch layer, decoy `delivery` grep hits, and three failing pytest modules (`test_ferry_queue`, `test_ferry_retry`, `test_notify_routes`). Agents must discover integration points via semantic search, not filename guessing.

Requirements: Qdrant + embed sidecar (`./scripts/lane-ctl.sh start vector`), work lane (:8002) + coder lane (:8001) on demand (one GPU lane at a time). Not part of the default GX10 suite.

### Vector test pyramid

| Layer | Marker | Command | GPU |
|-------|--------|---------|-----|
| Unit | — | `pytest tests/unit -q` | no |
| Vector stack only | `vector_live` | `VECTOR_LIVE=1 pytest -m vector_live -q` | no vLLM |
| Capability (per story) | `agent_capability` | `VECTOR_AGENT_LIVE=1 VLLM_LIVE=1 pytest tests/agent_live/capability/ -m "vector_agent_live and agent_capability" -v` | yes (~30–40 min / story) |
| Integration nightly | `agent_integration` + `nightly` | `VECTOR_AGENT_LIVE=1 VLLM_LIVE=1 pytest tests/agent_live/integration/ -m "vector_agent_live and agent_integration and nightly" -v` | yes (~1–1.5h) |
| Trap (adversarial) | `agent_trap` | `VECTOR_AGENT_LIVE=1 VLLM_LIVE=1 pytest tests/agent_live/trap/ -m agent_trap -v` | yes (SOFT; `VECTOR_TRAP_STRICT=1` to hard-fail) |

`from-prompt` integration backlog runs **2 stories** (queue + retry). Trap tier optionally runs full 3-story backlog including REST trap on `vector_repo`.

Chained workspaces: story N+1 starts from the workspace after story N ships (`prepare_chained_workspace`).

## Manual test commands

```bash
# Credential smoke (no pytest)
source .venv/bin/activate
python scripts/verify_integrations.py

# Live Jira + GitHub + API routes (no GPU)
INTEGRATION_LIVE=1 pytest tests/integration_live -m "integration_live and not vllm_live" -q

# Full cycle + real push/PR/Jira (~15–25 min)
INTEGRATION_LIVE=1 VLLM_LIVE=1 pytest tests/integration_live/test_ship_live_cycles.py::test_greeter_full_cycle_real_ship -q

# Single-agent live tests (real vLLM on fixture; no Jira/GitHub ship)
VLLM_LIVE=1 pytest tests/agent_live -m "agent_live and vllm_live" -q

# vLLM probes only
PREFLIGHT_LIVE=1 pytest -m preflight -q

# Vector stack (no vLLM)
./scripts/lane-ctl.sh start vector
VECTOR_LIVE=1 pytest -m vector_live -q

# Vector capability — single stories on vector_repo (~30–40 min each)
VECTOR_AGENT_LIVE=1 VLLM_LIVE=1 pytest tests/agent_live/capability/ -m "vector_agent_live and agent_capability" -v

# Vector integration nightly — 2-story from-prompt (mock ship, ~1–1.5h on GX10)
./scripts/lane-ctl.sh start vector
VECTOR_AGENT_LIVE=1 VLLM_LIVE=1 pytest tests/agent_live/integration/ -m "vector_agent_live and agent_integration and nightly" -v

# Vector trap tier — adversarial (SOFT; set VECTOR_TRAP_STRICT=1 to hard-fail)
VECTOR_AGENT_LIVE=1 VLLM_LIVE=1 pytest tests/agent_live/trap/ -m agent_trap -v

# Benchmark scorecard from JSON reports
python scripts/agent_scorecard.py

# Vector A/B full cycle (from-ticket, COMPLEX API ticket)
VECTOR_AGENT_LIVE=1 VLLM_LIVE=1 pytest tests/agent_live/test_sprint_vector_ab.py -m vector_agent_live -v
```

## What each live test does

| Test file | Creates in Jira | Creates on GitHub |
|-----------|-----------------|-------------------|
| `test_jira_roundtrip.py` | Issues `[sprint-crew-test]` | — |
| `test_backlog_jira.py` | Backlog stories | — |
| `test_api_routes.py` | Optional route smoke issue | — |
| `test_api_routes.py` (`from-prompt`) | — (BacklogRun only; Jira via `test_backlog_jira`) | — |
| `test_ship_real.py` | Issue + transition | Branch + PR |
| `test_ship_live_cycles.py` | Greeter (+ optional email) issues | Branch + PR after agent cycle |
| `tests/agent_live/` | — | — (fixture workspace only) |
| `tests/agent_live/capability/` | `USE_MOCK_INTEGRATIONS=true` (default) | — (per-story vector_repo cycles + story3 on integration fixture) |
| `tests/agent_live/integration/test_from_prompt_2story.py` | Mock | — (2-story from-prompt nightly gate + Qdrant) |
| `tests/agent_live/trap/` | Mock | — (stdlib shadow / 3-story trap; SOFT by default) |
| `tests/agent_live/test_sprint_vector_ab.py` | Mock | — (vector on/off A/B on greeter fixture) |
| `tests/unit/agents/` | — | — (unit, mock LLM only) |

## Cleanup

**Jira:** filter issues by summary prefix `[sprint-crew-test]` and close/delete manually in the sandbox project.

**GitHub:** ship tests close PRs and delete `feature/{TICKET-KEY}` branches in `finally` blocks. Verify in the repo if a run was interrupted.

**Workspaces:** removed from `SPRINT_WORKSPACE_BASE` by fixtures; orphaned dirs can be deleted manually.

Do not commit `.env` or tokens.
