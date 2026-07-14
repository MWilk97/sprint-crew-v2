from __future__ import annotations

import re
from pathlib import Path

from sprint_crew.config import get_settings
from sprint_crew.orchestrator.repo_manifest import build_repo_manifest, format_repo_manifest
from sprint_crew.schemas.session import AgentEvent
from sprint_crew.schemas.ticket import JiraTicket
from sprint_crew.tools import READONLY_TOOLS, build_registry
from sprint_crew.vector.indexer import should_index_workspace, should_use_vector
from sprint_crew.vector.search import SearchHit, format_search_hits, semantic_search

_GREP_STOPWORDS = frozenset({"with", "from", "that", "this", "into", "when", "have"})


def _keyword_grep_block(workspace: Path, ticket: JiraTicket | None, query_text: str) -> str:
    if ticket is None and not query_text.strip():
        return ""
    keywords: list[str] = []
    if ticket is not None:
        keywords.extend(
            word
            for word in re.findall(r"[A-Za-z_]{4,}", ticket.summary)
            if word.lower() not in _GREP_STOPWORDS
        )
    if not keywords and query_text.strip():
        keywords.extend(
            word
            for word in re.findall(r"[A-Za-z_]{4,}", query_text)
            if word.lower() not in _GREP_STOPWORDS
        )
    if not keywords:
        return ""
    registry = build_registry(READONLY_TOOLS)
    pattern = keywords[0]
    result = registry.dispatch("grep", {"pattern": pattern, "path": "."}, workspace_root=workspace)
    output = result.output if result.ok else f"[grep error] {result.output}"
    return f"=== keyword_grep (deterministic, pattern={pattern!r}) ===\n{output}"


def enrich_repo_context_with_hits(
    workspace: Path,
    session_id: str,
    query_text: str,
    *,
    ticket: JiraTicket | None = None,
) -> tuple[str, list[SearchHit]]:
    """Gather repo context, manifest, hybrid retrieval, and semantic pre-search."""
    from sprint_crew.orchestrator.repo_context import gather_repo_context

    base = gather_repo_context(workspace, ticket)
    parts = [base]
    hits: list[SearchHit] = []

    manifest = build_repo_manifest(workspace)
    if manifest:
        parts.append(
            "=== repo_manifest (all indexable files — paths must match) ===\n"
            + format_repo_manifest(manifest)
        )

    grep_block = _keyword_grep_block(workspace, ticket, query_text)
    if grep_block:
        parts.append(grep_block)

    if not should_use_vector(ticket=ticket, prompt=query_text):
        return "\n\n".join(parts), hits
    if not should_index_workspace(workspace, prompt=query_text, ticket=ticket):
        return "\n\n".join(parts), hits

    settings = get_settings()
    hits = semantic_search(session_id, query_text, top_k=settings.vector_top_k)
    if hits:
        parts.append(
            "=== pre_search (orchestrator pre-fetched — verify with read_file/grep before planning) ===\n"
            + format_search_hits(hits)
        )

    return "\n\n".join(parts), hits


def enrich_repo_context(
    workspace: Path,
    session_id: str,
    query_text: str,
    *,
    ticket: JiraTicket | None = None,
) -> str:
    text, _hits = enrich_repo_context_with_hits(workspace, session_id, query_text, ticket=ticket)
    return text


def pre_search_agent_event(query: str, hits: list[SearchHit]) -> AgentEvent:
    return AgentEvent(
        agent="orchestrator",
        event_type="pre_search",
        summary=f"pre_search: {len(hits)} hits",
        detail={
            "hits": len(hits),
            "query": query[:500],
            "top_paths": [hit.path for hit in hits[:5]],
        },
    )
