# ADR 0011 — Web console off GX10

**Status: Archived (Proposed, never implemented as its own doc).**

The content was folded into [archive/HISTORY.md](../archive/HISTORY.md#target-console-collapsed-from-vision--roadmap--adr-0011)
when the docs were consolidated to one owner per fact. This stub exists because the decision
is still cited from live code and contracts — `config.py`, `api/app.py`, `api/console.py`,
`schemas/console.py`, `.env.example` and ADR 0014 all reference "ADR 0011" for the CORS and
API-boundary rationale, and those citations previously pointed at a deleted file.

**The decision, in one line:** the browser console lives in a separate repo on a non-GX host
so GX10 unified memory stays available for inference; the FastAPI API is the sole UI contract
boundary, and the browser never talks to the vLLM lanes directly.

That is what the CORS configuration (`CONSOLE_CORS_ORIGINS`) and the shared-bearer auth
(`CONSOLE_API_TOKEN`) exist to serve. See [ADR 0012](0012-plan-code-modes-and-clarify.md) for
the console modes built on top of it.
