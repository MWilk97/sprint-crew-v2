# ADR 0016 — A durable per-repo index with a per-run overlay

**Status:** Accepted (2026-07-28) · **Milestone:** M8 · **Supersedes:** the per-session index lifecycle described in ADR 0011's console notes

## Context

Vector collections were keyed on a sprint session UUID and deleted at the end of every run
(`batch_cycle._cleanup_vector_indexes`). Two consequences:

- **Nothing was ever reusable.** Every run re-embedded the whole repository from scratch, and
  a console session had no index at all until a run started — which makes grounded clarify
  (M9) and codebase chat impossible by construction, not by omission.
- **The index went stale during the run that owned it.** Indexing was all-or-nothing on
  `HEAD`: same sha, skip entirely; different sha, delete and rebuild. Uncommitted work was
  never indexed, so every `semantic_search` after the Coder's first write answered from
  pre-edit content.

M8 moves the checkout to session-create, which forces the question the old lifecycle never
had to answer: if an index outlives a run, whose content is in it?

## Decision

**Two tiers of collection.**

| Tier | Key | Holds | Lifetime |
|---|---|---|---|
| Shared | repository (`repo_key` = hash of the normalised remote URL) | the repository's committed state | durable, LRU-capped at `VECTOR_MAX_COLLECTIONS` |
| Overlay | run scope id (console prep, backlog planning, or sprint session) | files whose content differs from the shared tier | deleted when the run ends |

Search takes an ordered list of collections and reads overlay first. **An overlay hit for a
path suppresses that path's shared hits**, so a run always sees its own edits and never the
stale committed version of a file it has already changed.

**Only a pristine checkout writes the shared tier.** A fresh clone *is* the repository's
committed state; a chained story workspace carries the previous story's commits, which are
that run's business and would otherwise leak into every other session's search results.

**Overlay membership is decided by content hash, not by git.** `overlay_paths` compares the
workspace's per-file hashes against the shared collection's manifest. One rule covers
uncommitted edits, chained commits and new files, and it degrades correctly: with an empty
shared collection every file is an overlay file, which is exactly the old per-run full index.

**Re-index is incremental.** `vector/manifest.py` keeps `path -> content_hash` per collection
in the existing sqlite file; `plan_reindex` diffs it into added/changed/deleted. Changed and
deleted files have their points dropped before new ones are written, because re-chunking can
produce fewer chunks than a file had and an upsert only overwrites the ids it writes.

## Consequences

- A second session on the same repository starts warm. The first one pays the embedding cost.
- Qdrant is now bounded by an LRU cap rather than by deletion-after-use, and by a startup
  sweep that drops prefixed collections the manifest does not know — which is also how the
  session-keyed collections from before this ADR get collected.
- `point_id` is scoped by collection, not by session, so the same chunk keeps its id across
  sessions. Collections written before this change have incompatible ids; they are orphans
  and the sweep drops them.
- A file deleted since the shared index was built is not represented in the overlay, so a
  stale hit is possible until the shared tier is rebuilt. Accepted: the agents' prompts
  already require verifying hits with `read_file`/`grep`.
- The manifest is durable state that tests must isolate (`SPRINT_SESSION_DB`), or a second
  run of the same test sees the first one's index and asserts nothing.

## Alternatives rejected

- **One shared collection, dirty files written straight into it.** Simplest to build, but one
  session's uncommitted work becomes visible to every other session on that repo, and a
  cancelled run leaves the shared index poisoned until the next commit-level rebuild.
- **Keep deleting after every run.** Preserves isolation and nothing else; it is precisely
  what makes a warm session impossible.
- **Scroll Qdrant for the previous file state instead of a manifest.** Same answer, one
  network round-trip per index, and untestable without the service running (invariant 10).
