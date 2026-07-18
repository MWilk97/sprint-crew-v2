# Architecture overview

Sprint Crew v2 automates the software delivery loop for a single ticket at a time.

## Components

| Layer | Responsibility |
|-------|----------------|
| **API** (`api/app.py`, `api/console.py`) | HTTP entry points: `/sprint/*` (from-prompt, from-ticket, session status, approve) and `/v1/console/*` (interactive console MVP) |
| **Orchestrator** (`orchestrator/`) | Workspace prep, git push, Jira transitions, GitHub PR creation |
| **LangGraph** (`graph/pipeline.py`) | State machine: plan → code → test → review → ship node |
| **Agents** (`agents/`) | TechLead, Coder, Tester, Formatter, Reviewer, ScrumMaster |
| **Tools** (`tools/`) | read_file, write_file, apply_patch, grep, run_command (allowlisted) |
| **Vector** (`vector/`) | Optional Qdrant index + semantic search for non-TRIVIAL tickets (SIMPLE and COMPLEX) |
| **Integrations** (`integrations/`) | Jira and GitHub clients (real + mock) |

## Inference lanes

Two vLLM containers share one GPU with **mutual exclusion**:

- **Coder lane** (:8001) — tool-loop coding and test writing
- **Work lane** (:8002) — structured JSON (TaskPlan, ReviewOutcome, BacklogPlan) and TechLead exploration

Lanes start on demand via `scripts/lane-ctl.sh` and stop before swapping models.

**Model serving:** vLLM container flags and HF model IDs live in [`infra/docker-compose.yml`](../infra/docker-compose.yml). [`infra/models.yaml`](../infra/models.yaml) is the Python client config (ports, `served_name`); YAML `tool_call_parser` fields are documentation-only.

## Side-effect boundary

LLM tools are read/write within the workspace only. Git push, branch creation, Jira status changes, and PR opening happen in Python orchestrator code — never inside agent tool handlers.

## Merge gate

A cycle completes when:

```python
review.passed and review.tests_passed and no blocker findings and plan coverage satisfied
```

Human approval is required before merging to main (see [adr/0010-manual-merge-gate.md](adr/0010-manual-merge-gate.md)).

## Test strategy

Fast unit tests mock all LLM calls. Opt-in live tiers exercise real Jira/GitHub (sandbox) and real vLLM (GPU). See [integration-testing.md](integration-testing.md).
