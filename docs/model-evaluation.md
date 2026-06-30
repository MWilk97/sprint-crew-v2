# Model evaluation (Preflight gate)

Production = rows with A+B+C+D = PASS.

| Model | Slot | A | B | C | D | TTFT ms | Tok/s | Verdict |
|-------|------|---|---|---|---|---------|-------|---------|
| QuantTrio/Qwen3-Coder-30B-A3B-Instruct-AWQ | Coder :8001 | PASS | PASS | — | — | | | baseline |
| Qwen/Qwen3-14B | Planner :8002 | n/a | n/a | | — | | | |
| google/gemma-4-12B-it | Judge :8003 | n/a | n/a | | n/a | | | |

Fill C/D after `scripts/probe_json.py` and `scripts/smoke_coder.py`.
