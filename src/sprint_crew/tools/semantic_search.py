from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, Field

from sprint_crew.config import get_settings
from sprint_crew.tools._safety import UnsafePathError, resolve_safe_path
from sprint_crew.tools.base import ToolResult
from sprint_crew.vector.search import format_search_hits, semantic_search


class SemanticSearchArgs(BaseModel):
    query: str = Field(..., min_length=1)
    path_prefix: str | None = None
    top_k: int | None = Field(default=None, ge=1, le=20)
    chunk_kind: str | None = None


class SemanticSearchTool:
    name = "semantic_search"
    description = (
        "Semantic code search over the indexed workspace. "
        "Use for concept-level discovery; verify hits with read_file or grep."
    )
    args_schema = SemanticSearchArgs

    def __init__(self, *, session_id: str | None = None) -> None:
        self._session_id = session_id

    def execute(self, args: BaseModel, *, workspace_root: Path) -> ToolResult:
        assert isinstance(args, SemanticSearchArgs)
        settings = get_settings()
        if not settings.vector_index_enabled:
            return ToolResult(ok=True, output="(semantic search unavailable)")

        if args.path_prefix:
            try:
                resolve_safe_path(args.path_prefix, root=workspace_root.resolve(strict=False))
            except UnsafePathError as exc:
                return ToolResult(ok=False, output=str(exc), error="unsafe path")

        session_id = self._session_id or workspace_root.name
        hits = semantic_search(
            session_id,
            args.query,
            top_k=args.top_k,
            path_prefix=args.path_prefix,
            chunk_kind=args.chunk_kind,
        )
        if args.path_prefix:
            prefix = args.path_prefix.lstrip("./")
            hits = [h for h in hits if h.path.startswith(prefix)]

        body = format_search_hits(hits)
        return ToolResult(ok=True, output=body, data={"hits": len(hits)})


def semantic_search_tool(session_id: str | None = None) -> SemanticSearchTool:
    return SemanticSearchTool(session_id=session_id)
