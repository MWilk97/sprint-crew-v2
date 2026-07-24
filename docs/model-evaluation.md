# Model evaluation (Preflight gate)

Production = rows with A+B+C+D = PASS.

| Model | Slot | A | B | C | D | TTFT ms | Tok/s | Verdict |
|-------|------|---|---|---|---|---------|-------|---------|
| poolside/Laguna-S-2.1-NVFP4 | Coder :8001 | PASS | PASS | — | PASS | | | production candidate — Laguna S 2.1 (max_len 131072, gpu util 0.85, poolside_v1 tool+reasoning parser, T=0.7/top_p=0.95/top_k=20; thinking OFF early, escalated per-request from coder attempt 2 with 1800s timeout to absorb the reasoning trace that overran the default deadline; image `timothystewart6/vllm-gb10:v0.25.1-gb10.2`) |
| gdubicki/Qwen3-Coder-Next-NVFP4-GB10 | Coder :8001 | ? | ? | — | ? | | | rollback — prior NVFP4 coder (max_len 131072, gpu util 0.85, Marlin; ungated mirror of gated saricles/… ckpt) |
| Qwen/Qwen3-Coder-Next-FP8 | Coder :8001 | PASS | PASS | — | PASS | | | rollback baseline (max_len 12288, gpu util 0.78) |
| NVFP4/Qwen3-30B-A3B-Thinking-2507-FP4 | Work :8002 | n/a | n/a | ? | — | | | candidate — preflight pending re-run (max_len 131072, gpu util 0.50, qwen3_moe text-only, hermes parser, prep + TechLead + reviewer) |
| RedHatAI/Qwen3.6-35B-A3B-NVFP4 | Work :8002 | n/a | n/a | | — | | | prior candidate (needed tokenizer patch + Mamba flag) |
| Qwen/Qwen3-14B | Work :8002 | n/a | n/a | PASS | — | | | rollback baseline (max_len 16384, gpu util 0.40) |

Fill D after `scripts/smoke_cycle.py --coder-only`; full cycle after `scripts/smoke_cycle.py`.

## Rollback (Coder FP8 baseline)

If Coder-Next NVFP4 fails to init (OOM, vLLM parser error), revert `infra/docker-compose.yml` and `infra/models.yaml` to the FP8 baseline (matches the rollback block in `infra/models.yaml`):

```yaml
model_id: Qwen/Qwen3-Coder-Next-FP8
served_name: qwen3-coder-next
max_model_len: 12288
gpu_memory_utilization: 0.78
```

Then: `./scripts/lane-ctl.sh stop coder && ./scripts/lane-ctl.sh start coder`.

Review runs on the **work lane** (:8002) — there is no separate judge lane.

## Test layers

| Layer | Marker / command | Requires |
|-------|------------------|----------|
| Unit | `pytest tests/unit -q` | venv only |
| Integration smoke (no vLLM) | `scripts/verify_integrations.py` | real Jira/GitHub |
| Manual reference baseline | `scripts/smoke_cycle.py` | Docker + GPU |
| Model comparison | `scripts/benchmark_pipeline.py` | Docker + GPU |

## Comparing models

1. Edit `infra/models.yaml` (lane `model_id` / `served_name`).
2. Restart affected lane: `scripts/lane-ctl.sh stop work && scripts/lane-ctl.sh start work`.
3. Run `scripts/smoke_cycle.py` for a real end-to-end cycle on the new model.
4. Or run all scenarios: `python scripts/benchmark_pipeline.py` → JSON under `benchmarks/results/`.
