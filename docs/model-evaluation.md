# Model evaluation (Preflight gate)

Production = rows with A+B+C+D = PASS.

| Model | Slot | A | B | C | D | TTFT ms | Tok/s | Verdict |
|-------|------|---|---|---|---|---------|-------|---------|
| saricles/Qwen3-Coder-Next-NVFP4-GB10 | Coder :8001 | ? | ? | — | ? | | | candidate — preflight pending re-run (max_len 131072, gpu util 0.85, Marlin NVFP4) |
| Qwen/Qwen3-Coder-Next-FP8 | Coder :8001 | PASS | PASS | — | PASS | | | rollback baseline (max_len 12288, gpu util 0.78) |
| NVFP4/Qwen3-30B-A3B-Thinking-2507-FP4 | Work :8002 | n/a | n/a | ? | — | | | candidate — preflight pending re-run (max_len 131072, gpu util 0.50, qwen3_moe text-only, hermes parser, prep + TechLead + reviewer) |
| RedHatAI/Qwen3.6-35B-A3B-NVFP4 | Work :8002 | n/a | n/a | | — | | | prior candidate (needed tokenizer patch + Mamba flag) |
| Qwen/Qwen3-14B | Work :8002 | n/a | n/a | PASS | — | | | rollback baseline (max_len 16384, gpu util 0.40) |

Fill D after `scripts/smoke_cycle.py --coder-only`; full cycle after `scripts/smoke_cycle.py`.

## Rollback (Coder AWQ baseline)

If Coder-Next fails to init (OOM, vLLM parser error), revert `infra/docker-compose.yml` and `infra/models.yaml`:

```yaml
model_id: QuantTrio/Qwen3-Coder-30B-A3B-Instruct-AWQ
served_name: qwen3-coder-30b
gpu_memory_utilization: 0.28
```

Then: `./scripts/lane-ctl.sh stop coder && ./scripts/lane-ctl.sh start coder` — AWQ baseline had A+B+D PASS.

Review runs on the **work lane** (:8002) — there is no separate judge lane.

## Test layers

| Layer | Marker / command | Requires |
|-------|------------------|----------|
| Unit | `pytest tests/unit -q` | venv only |
| Sandbox API + Jira/GitHub | `INTEGRATION_LIVE=1 pytest tests/integration_live -m "integration_live and not vllm_live" -q` | `.env` credentials |
| Live vLLM ship cycle | `./scripts/run_gx10_test_suite.sh` | Docker + GPU lanes |
| Integration smoke (no vLLM) | `scripts/verify_integrations.py` | real Jira/GitHub |
| Manual reference baseline | `scripts/smoke_cycle.py` | Docker + GPU |
| Model comparison | `scripts/benchmark_pipeline.py` | Docker + GPU |

## Comparing models

1. Edit `infra/models.yaml` (lane `model_id` / `served_name`).
2. Restart affected lane: `scripts/lane-ctl.sh stop work && scripts/lane-ctl.sh start work`.
3. Run `./scripts/run_gx10_test_suite.sh` (preflight + greeter ship_live).
4. Or run all scenarios: `python scripts/benchmark_pipeline.py` → JSON under `benchmarks/results/`.
