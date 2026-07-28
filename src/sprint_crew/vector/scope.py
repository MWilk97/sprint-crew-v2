"""Which collections a workspace reads and writes (roadmap M8).

Two tiers. The **shared** collection is keyed by repository and holds its committed state;
it outlives every run and is what makes a second session on the same repo warm. The
**overlay** is keyed by the run and holds only the files whose content differs from that
committed baseline — the edits an agent is making right now. Search reads overlay first,
so a run always sees its own work, and no other session ever does.

A workspace names its own repository through its git remote, so nothing has to thread a
``repo_url`` down through the graph to work out where its index lives.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from sprint_crew.vector.chunker import file_hashes
from sprint_crew.vector.manifest import index_manifest
from sprint_crew.vector.store import collection_for_repo, collection_for_run, repo_key


@dataclass(frozen=True)
class IndexScope:
    repo_key: str
    shared: str
    overlay: str | None = None

    @property
    def collections(self) -> tuple[str, ...]:
        """Search order: the overlay's view of a file supersedes the shared one."""
        return (self.overlay, self.shared) if self.overlay else (self.shared,)


def git_remote_url(workspace: Path) -> str | None:
    proc = subprocess.run(
        ["git", "remote", "get-url", "origin"],
        cwd=workspace,
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        return None
    return (proc.stdout or "").strip() or None


@lru_cache(maxsize=64)
def _repo_key_for_workspace(resolved: str) -> str:
    # A workspace's remote does not change under it, so this is a per-path constant; the
    # cache keeps a subprocess out of every semantic_search tool call.
    return repo_key(git_remote_url(Path(resolved)))


def index_scope_for(workspace: Path, *, run_scope_id: str | None = None) -> IndexScope:
    resolved = workspace.resolve()
    key = _repo_key_for_workspace(str(resolved))
    return IndexScope(
        repo_key=key,
        shared=collection_for_repo(key),
        overlay=collection_for_run(run_scope_id) if run_scope_id else None,
    )


def overlay_hashes(workspace: Path, base_collection: str) -> dict[str, str]:
    """``path -> content hash`` for the files that differ from the shared collection.

    Content hashes rather than ``git status``: this has to catch a chained workspace's
    committed work and a Coder's uncommitted edits with one rule, and it degrades
    correctly when the shared collection is empty (everything is an overlay file, which
    is exactly the old per-run full index). Files deleted since the baseline are not
    represented — a stale hit is possible until the shared index is rebuilt, which the
    agents' verify-with-read_file instruction already covers.

    Returns the hashes, not just the paths, because the caller feeds them straight to
    ``index_workspace``: this runs after every Coder node, and hashing the whole tree
    twice per pass was the cost of handing on only the names.
    """
    baseline = index_manifest().file_hashes(base_collection)
    return {
        path: digest
        for path, digest in file_hashes(workspace).items()
        if baseline.get(path) != digest
    }
