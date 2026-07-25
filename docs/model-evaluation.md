# Model evaluation

Production = rows with A+B+C+D = PASS. Past candidates live in [archive/HISTORY.md](archive/HISTORY.md).

Probes (A–C are `scripts/probe_vllm_tools.py --lane <coder|work>`):

| Probe | Check | Lane |
|-------|-------|------|
| A | `tool_probe` — one `read_file` tool_call, no prose leakage | Coder |
| B | `multi_tool_probe` — multi-step tool_calls under the coder system prompt | Coder |
| C | `techlead_probe` — read-only exploration tool_calls | Work |
| D | `scripts/smoke_cycle.py --coder-only` — real Coder+Formatter cycle | Coder |
| E | `scripts/probe_interpreter.py` — clarify questions carry a recommendation, a clear request yields none, and (`--image`) the lane accepts image input | Work |

| Model | Slot | A | B | C | D | E | TTFT ms | Tok/s | Verdict |
|-------|------|---|---|---|---|---|---------|-------|---------|
| poolside/Laguna-S-2.1-NVFP4 | Coder :8001 | PASS | PASS | — | PASS | — | | | production — Laguna S 2.1 (max_len 131072, gpu util 0.85, poolside_v1 tool+reasoning parser, T=0.7/top_p=0.95/top_k=20; thinking OFF early, escalated per-request from coder attempt 2 with 1800s timeout; image `timothystewart6/vllm-gb10:v0.25.1-gb10.2`) |
| gdubicki/Qwen3-Coder-Next-NVFP4-GB10 | Coder :8001 | ? | ? | — | ? | — | | | rollback — prior NVFP4 coder (max_len 131072, gpu util 0.85, Marlin) |
| Qwen/Qwen3-Coder-Next-FP8 | Coder :8001 | PASS | PASS | — | PASS | — | | | rollback baseline (max_len 12288, gpu util 0.78) |
| RedHatAI/Qwen3.6-35B-A3B-NVFP4 | Work :8002 | PASS | PASS | PASS | — | PASS | | ~30 | production — natively multimodal, hosts Interpreter (max_len 131072, gpu util 0.65, kv fp8, `qwen3_coder` tool parser, `qwen3` reasoning parser, `--max-num-batched-tokens 8192`, image `timothystewart6/vllm-gb10:v0.25.1-gb10.2`). KV cache 5.04M tokens, 38.46x concurrency @131k; ~30 tok/s decode under `--enforce-eager` |
| NVFP4/Qwen3-30B-A3B-Thinking-2507-FP4 | Work :8002 | n/a | n/a | ? | — | — | | | rollback — prior work model, text-only (max_len 131072, gpu util 0.50, qwen3_moe, hermes parser, needs `scripts/patch_work_quant.py`) |
| Qwen/Qwen3-14B | Work :8002 | n/a | n/a | PASS | — | — | | | rollback baseline (max_len 16384, gpu util 0.40) |

Work-lane notes for Qwen3.6:

- Needs **transformers 5.x** — the NGC `vllm:26.04-py3` image (vLLM 0.19 / transformers 4.57) fails at tokenizer load with `Tokenizer class TokenizersBackend does not exist`.
- Mamba hybrid: prefix caching forces Mamba cache `align` mode, which asserts `block_size (2096) <= max_num_batched_tokens`. The 2048 default fails engine init.
- The chat template emits `<tool_call><function=..><parameter=..>`, so the parser moved from `hermes` to `qwen3_coder`. Probes A–C re-run green on that parser.

### Probe E results (2026-07-25, GX10)

| Case | Latency | Outcome |
|------|---------|---------|
| Vague prompt, thinking **off** | 14.9 s | 1 grounded question, confidence 0.7 |
| Vague prompt, thinking **on** | 177.4 s | 3 questions, one of them asking which backend stack, confidence 0.5 |
| Vague prompt (repeat, thinking off) | 23.7 s | 2 questions, both with recommendations |
| Clear prompt | 8.3 s | **zero questions**, confidence 0.95 — went straight to `ready` |
| Image (480x320 PNG) | 1.8 s | Colours, layout, and corner placement all described correctly |

**Thinking stays off for clarify** (`INTERPRETER_THINKING_ENABLED=false`): 12x slower *and* vaguer,
because the reasoning trace burns the budget before the model commits to a question.

The image case also has a trap worth remembering: with thinking on and a small `max_tokens`,
the reasoning parser moves the whole trace into `reasoning_content` and `content` comes back
**empty**. That looks exactly like a broken vision tower and is not one.

Clarify latency is **~15–24 s**, against a target of ~15 s. The remaining lever is
`--enforce-eager` on the work lane (kept for memory safety), which disables CUDA graphs and
holds decode at ~30 tok/s.

### Full-cycle verification (probe D, 2026-07-25)

`scripts/smoke_cycle.py` on `fixtures/repo` reached `awaiting_human` with the merge gate
accepting (`passed=True tests_passed=True`) and the story's acceptance test green. Coder ran
on Laguna, **Formatter and Reviewer on the new Qwen3.6 work model**.

**Known coverage gap:** the greeter ticket is TRIVIAL, so TechLead took the template
fast-path and its `tool_loop` never ran. TechLead planning on the `qwen3_coder` parser is
therefore still unexercised end-to-end — probes A–C cover the tool_call mechanism it relies
on, but not the planning loop itself. The 3-story trap gate is what closes this.

Fill D after `scripts/smoke_cycle.py --coder-only`; full cycle after `scripts/smoke_cycle.py`.

## Rollback (Coder FP8 baseline)

If Coder NVFP4 fails to init (OOM, vLLM parser error), revert `infra/docker-compose.yml` and `infra/models.yaml` to the FP8 baseline (matches the rollback block in `infra/models.yaml`):

```yaml
model_id: Qwen/Qwen3-Coder-Next-FP8
served_name: qwen3-coder-next
max_model_len: 12288
gpu_memory_utilization: 0.78
```

Then: `./scripts/lane-ctl.sh stop coder && ./scripts/lane-ctl.sh start coder`.

Review runs on the **work lane** (:8002) — there is no separate judge lane.

## Comparing models

1. Edit `infra/models.yaml` (lane `model_id` / `served_name`).
2. Restart affected lane: `scripts/lane-ctl.sh stop work && scripts/lane-ctl.sh start work`.
3. Run `scripts/smoke_cycle.py` for a real end-to-end cycle on the new model.
4. Or run all scenarios: `python scripts/benchmark_pipeline.py` → JSON under `benchmarks/results/`.
