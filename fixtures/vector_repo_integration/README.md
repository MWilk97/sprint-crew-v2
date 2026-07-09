# Vector integration fixture

Duplicate of [`../vector_repo/`](../vector_repo/) used for **multi-story integration** and nightly GPU gates.

## Why a separate copy?

| Fixture | Used by |
|---------|---------|
| `vector_repo/` | Capability benchmarks, trap overlays, A/B vector tests |
| `vector_repo_integration/` | Chained from-prompt integration (`test_from_prompt_2story`) |

Keeping two trees avoids cross-contamination when integration tests mutate workspace state or overlay directories between runs.

## Stories

Same three-story arc as `vector_repo`: outbound queue → retry policy → notification REST routes. See [vector_repo/README.md](../vector_repo/README.md) for architecture terminology.
