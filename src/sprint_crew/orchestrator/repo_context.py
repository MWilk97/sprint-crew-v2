"""Repo context assembly and vector-retrieval orchestration.

Everything that decides *whether* and *how* to enrich planning context —
deterministic snapshot, manifest, keyword grep, semantic pre-search, and the
vector-indexing gate — lives here so the vector package stays a plain service
layer with no orchestrator imports.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

from sprint_crew.config import get_settings
from sprint_crew.orchestrator.complexity import (
    PromptComplexity,
    assess_prompt_complexity,
    assess_ticket_complexity,
)
from sprint_crew.orchestrator.repo_manifest import build_repo_manifest, format_repo_manifest
from sprint_crew.paths import paths_in_text
from sprint_crew.schemas.session import AgentEvent
from sprint_crew.schemas.ticket import JiraTicket
from sprint_crew.tools import READONLY_TOOLS, build_registry
from sprint_crew.vector.chunker import count_indexable_files
from sprint_crew.vector.indexer import IndexResult, index_workspace
from sprint_crew.vector.search import SearchHit, format_search_hits, semantic_search

log = logging.getLogger(__name__)

_MAX_FILE_SNIPPET = 4000

_GREP_STOPWORDS = frozenset({"with", "from", "that", "this", "into", "when", "have"})


def paths_from_ticket(ticket: JiraTicket) -> list[str]:
    text = "\n".join(
        [
            ticket.summary,
            ticket.description,
            ticket.acceptance_criteria,
        ]
    )
    return paths_in_text(text)[:8]


def gather_repo_context(workspace_root: Path, ticket: JiraTicket | None = None) -> str:
    """Deterministic repo snapshot via read-only tools (git status, listing, log)."""
    registry = build_registry(READONLY_TOOLS)
    parts: list[str] = []

    def run_tool(name: str, args: dict | None = None) -> str:
        result = registry.dispatch(name, args or {}, workspace_root=workspace_root)
        return result.output if result.ok else f"[{name} error] {result.output}"

    parts.append("=== git status ===")
    parts.append(run_tool("git_status"))
    parts.append("=== directory listing (.) ===")
    parts.append(run_tool("list_directory", {"path": "."}))
    parts.append("=== recent git log ===")
    parts.append(run_tool("git_log", {"n": 5}))

    if ticket is not None:
        for rel_path in paths_from_ticket(ticket):
            parts.append(f"=== read_file: {rel_path} ===")
            content = run_tool("read_file", {"path": rel_path})
            if len(content) > _MAX_FILE_SNIPPET:
                content = content[:_MAX_FILE_SNIPPET] + "\n... (truncated)"
            parts.append(content)

    readme = workspace_root / "README.md"
    if readme.exists() and (ticket is None or "README.md" not in paths_from_ticket(ticket)):
        parts.append("=== read_file: README.md ===")
        content = run_tool("read_file", {"path": "README.md"})
        if len(content) > _MAX_FILE_SNIPPET:
            content = content[:_MAX_FILE_SNIPPET] + "\n... (truncated)"
        parts.append(content)

    return "\n".join(parts)


def should_use_vector(
    *,
    ticket: JiraTicket | None = None,
    prompt: str | None = None,
) -> bool:
    settings = get_settings()
    if not settings.vector_index_enabled:
        return False

    if ticket is not None:
        return assess_ticket_complexity(ticket) != PromptComplexity.TRIVIAL

    if prompt is not None:
        return assess_prompt_complexity(prompt) != PromptComplexity.TRIVIAL

    return True


def should_index_workspace(
    workspace: Path,
    *,
    prompt: str | None = None,
    ticket: JiraTicket | None = None,
) -> bool:
    if not should_use_vector(ticket=ticket, prompt=prompt):
        return False
    return count_indexable_files(workspace) > 0


def maybe_index_workspace(
    workspace: Path,
    session_id: str,
    *,
    prompt: str | None = None,
    ticket: JiraTicket | None = None,
) -> IndexResult | None:
    """Build vector index when enabled and heuristics match; never raises."""
    if not should_index_workspace(workspace, prompt=prompt, ticket=ticket):
        return None
    try:
        return index_workspace(workspace, session_id)
    except Exception:
        log.warning(
            "Vector index build failed for session %s",
            session_id,
            exc_info=True,
        )
        return None


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
