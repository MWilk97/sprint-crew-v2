# Greeter fixture

Minimal Python repo used as the **smoke baseline** for sprint cycles.

## Contents

| File | Purpose |
|------|---------|
| `greeter.py` | Module under test — agents add `hello()` |
| `validators.py` | Email validation helper (email ship_live scenario) |
| `tests/test_greeter.py` | Greeter acceptance tests |
| `tests/test_validators.py` | Email validation tests |
| `simple_task_plan.json` | Static TaskPlan for `scripts/smoke_cycle.py --coder-only` |

## Usage

```bash
pytest -q                          # local sanity check
./scripts/smoke_cycle.py             # full cycle on temp clone
./scripts/smoke_cycle.py --coder-only
```

Cloned to a dedicated GitHub sandbox for manual `scripts/smoke_cycle.py` ship runs.
