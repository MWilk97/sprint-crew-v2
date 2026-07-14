from __future__ import annotations

from pathlib import Path

from sprint_crew.orchestrator.complexity import _paths_in_text
from sprint_crew.schemas.ticket import JiraTicket
from sprint_crew.tools import READONLY_TOOLS, build_registry

_MAX_FILE_SNIPPET = 4000


def paths_from_ticket(ticket: JiraTicket) -> list[str]:
    text = "\n".join(
        [
            ticket.summary,
            ticket.description,
            ticket.acceptance_criteria,
        ]
    )
    return _paths_in_text(text)[:8]


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
