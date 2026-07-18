# ADR 0011: Web console off the GX10

## Status

Proposed

## Context

Sprint Crew v2 has no user interface; the only entry points are the FastAPI endpoints. The [product vision](../vision/product-vision.md) calls for an interactive web console. The GX10 is memory-constrained — 128 GB unified memory, one vLLM lane loaded at a time — and its job is inference, not serving web traffic. Exposing the vLLM lanes (:8001, :8002) to a browser would also bypass the pipeline's schema contracts and safety gates.

## Decision

The web console lives in a **separate repository** and runs on a **non-GX host**. The browser talks **only** to this repo's FastAPI API; it **never** reaches the vLLM lanes directly. This repo remains the GX10 backend and the single source of pipeline behavior.

## Consequences

- GX10 memory and GPU stay dedicated to inference lanes
- The FastAPI API becomes the single contract boundary between UI and pipeline; UI needs drive API design, not the reverse
- The console can iterate (framework, hosting, release cadence) without touching this repo
- Authentication and CORS become API concerns to solve before the console ships (future work)
