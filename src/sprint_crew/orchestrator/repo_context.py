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
from sprint_crew.vector.indexer import (
    IndexResult,
    ProgressFn,
    enforce_collection_lru,
    index_workspace,
)
from sprint_crew.vector.scope import IndexScope, overlay_hashes
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


def maybe_index_shared(
    workspace: Path,
    scope: IndexScope,
    *,
    prompt: str | None = None,
    ticket: JiraTicket | None = None,
    on_progress: ProgressFn | None = None,
) -> IndexResult | None:
    """Refresh the repo-wide index from a pristine checkout; never raises.

    Only ever called with a workspace whose content *is* the repository's committed state —
    a fresh clone. A chained story workspace carries the previous story's commits, which
    belong in that run's overlay, not in what every other session reads.
    """
    if not should_index_workspace(workspace, prompt=prompt, ticket=ticket):
        return None
    try:
        result = index_workspace(
            workspace,
            scope.shared,
            repo_key=scope.repo_key,
            on_progress=on_progress,
        )
    except Exception:
        log.warning("Vector index build failed for %s", scope.shared, exc_info=True)
        return None
    # Only the shared tier is capped: overlays are deleted wholesale when their run ends,
    # so charging a mid-run refresh for an LRU pass would evict a real repo index to make
    # room for something already scheduled for deletion.
    enforce_collection_lru()
    return result


def maybe_index_overlay(workspace: Path, scope: IndexScope) -> IndexResult | None:
    """Refresh a run's overlay with whatever now differs from the shared baseline.

    Called at session start and again after the Coder writes, which is what stops the
    index going stale mid-run — the failure the all-or-nothing sha check could not see.
    """
    if scope.overlay is None or not get_settings().vector_index_enabled:
        return None
    try:
        return index_workspace(
            workspace,
            scope.overlay,
            repo_key=scope.repo_key,
            hashes=overlay_hashes(workspace, scope.shared),
        )
    except Exception:
        log.warning("Vector overlay build failed for %s", scope.overlay, exc_info=True)
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
    scope: IndexScope,
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
    hits = semantic_search(scope.collections, query_text, top_k=settings.vector_top_k)
    if hits:
        parts.append(
            "=== pre_search (orchestrator pre-fetched — verify with read_file/grep before planning) ===\n"
            + format_search_hits(hits)
        )

    return "\n\n".join(parts), hits


def enrich_repo_context(
    workspace: Path,
    scope: IndexScope,
    query_text: str,
    *,
    ticket: JiraTicket | None = None,
) -> str:
    text, _hits = enrich_repo_context_with_hits(workspace, scope, query_text, ticket=ticket)
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
