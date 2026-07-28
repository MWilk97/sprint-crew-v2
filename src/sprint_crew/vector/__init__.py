"""Vector indexing and semantic search for sprint workspaces."""

from sprint_crew.vector.indexer import IndexResult, delete_index, index_workspace
from sprint_crew.vector.search import SearchHit, format_search_hits, semantic_search
from sprint_crew.vector.store import collection_for_repo, collection_for_run, repo_key

__all__ = [
    "IndexResult",
    "SearchHit",
    "collection_for_repo",
    "collection_for_run",
    "delete_index",
    "format_search_hits",
    "index_workspace",
    "repo_key",
    "semantic_search",
]
