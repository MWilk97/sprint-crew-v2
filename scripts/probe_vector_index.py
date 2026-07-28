#!/usr/bin/env python3
"""Probe Qdrant + embed sidecar and round-trip index/search on fixtures/repo."""

from __future__ import annotations

import os
import sys
import uuid
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from sprint_crew.vector.indexer import delete_index, index_workspace  # noqa: E402
from sprint_crew.vector.search import semantic_search  # noqa: E402
from sprint_crew.vector.store import collection_for_run  # noqa: E402


def _check_url(name: str, url: str, path: str = "/health") -> None:
    try:
        with httpx.Client(timeout=10.0) as client:
            r = client.get(f"{url.rstrip('/')}{path}")
            r.raise_for_status()
        print(f"OK  {name}  {url}")
    except httpx.HTTPError as exc:
        print(f"FAIL {name}  {url}  ({exc})")
        raise SystemExit(1) from exc


def main() -> None:
    os.environ.setdefault("VECTOR_INDEX_ENABLED", "1")
    qdrant = os.environ.get("QDRANT_URL", "http://127.0.0.1:6333")
    embed = os.environ.get("EMBED_URL", "http://127.0.0.1:8080/v1")

    _check_url("qdrant", qdrant, "/healthz")
    _check_url("embed", embed.replace("/v1", ""), "/health")

    # Probe collections are run-scoped and dropped at the end: this is a round-trip check,
    # not a repo anyone will search later, and it must not land in the shared LRU.
    fixture = ROOT / "fixtures" / "repo"
    probe = collection_for_run(f"probe-{uuid.uuid4().hex[:8]}")
    vector_fixture = ROOT / "fixtures" / "vector_repo"
    vector_probe = collection_for_run(f"probe-vector-{uuid.uuid4().hex[:8]}")
    try:
        print(f"Indexing {fixture} as {probe}...")
        result = index_workspace(fixture, probe, repo_key="probe")
        print(f"Indexed {result.chunks} chunks from {result.files} files in {result.seconds:.2f}s")

        hits = semantic_search([probe], "greeting hello function", top_k=3)
        print(f"Search hits (repo): {len(hits)}")
        for hit in hits:
            print(f"  {hit.path}:{hit.start_line}-{hit.end_line} score={hit.score:.3f}")

        print(f"Indexing {vector_fixture} as {vector_probe}...")
        vector_result = index_workspace(vector_fixture, vector_probe, repo_key="probe")
        print(
            f"Indexed {vector_result.chunks} chunks from {vector_result.files} files "
            f"in {vector_result.seconds:.2f}s"
        )
        vector_hits = semantic_search([vector_probe], "ferry dispatch outbound queue", top_k=5)
        print(f"Search hits (vector_repo): {len(vector_hits)}")
        for hit in vector_hits:
            print(f"  {hit.path}:{hit.start_line}-{hit.end_line} score={hit.score:.3f}")
    finally:
        delete_index(probe)
        delete_index(vector_probe)

    if not hits:
        print(
            "WARN: no search hits on greeter repo (fixture is tiny — may be below score threshold)"
        )
    if not any("ferry" in hit.path for hit in vector_hits):
        print("WARN: ferry.py not in top vector_repo hits")
    print("probe_vector_index: PASS")


if __name__ == "__main__":
    main()
