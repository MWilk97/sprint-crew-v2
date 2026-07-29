# ADR 0020 — The local repo bridge is a deployment move, not a remoting layer

**Status:** Accepted (2026-07-29) · **Milestone:** M12 · **Builds on:** [0011](0011-web-console-off-gx.md) (console off the GX10), [0014](0014-run-queue-and-cancel.md) (run slot and cancel), [0016](0016-durable-repo-index.md) (durable per-repo index)

## Context

The roadmap reserved M12 as a sketch: let the agent edit a checkout on the user's own
machine rather than a server-side clone. It listed three shapes in order of preference —
a thin local CLI agent over a WebSocket, backend-as-library, and a working-tree sync
layer — and deliberately declined to choose, on the grounds that deciding early would be
guessing.

It also named two prerequisites: that the tool layer go through an abstraction rather than
direct filesystem calls, and that auth become real rather than a shared token.

An inventory of what actually touches the working tree was taken before choosing. It
changes the answer.

**The tool layer is already the clean part.** Every tool takes `workspace_root: Path`,
goes through one `ToolRegistry.dispatch`, and validates paths with `resolve_safe_path`.
That is close to a remotable interface, exactly as the roadmap guessed.

**The tool layer is not where the coupling lives.** Five subsystems bypass it entirely:

- `run_command` and `run_acceptance_tests` get their cancel semantics from
  `asyncio.create_subprocess_exec` plus a process-group `SIGTERM`/`SIGKILL` in the *same
  process* that awaits them (`proc.py:31-65`). "Stop kills the child" is a documented
  invariant (AGENTS.md §3.1, [ADR 0014](0014-run-queue-and-cancel.md)).
- `diff_capture.capture_snapshot` diffs untracked files with `git diff --no-index`, which
  needs their actual bytes on disk, not committed history (`orchestrator/diff_capture.py:155-165`).
- Vector indexing walks the whole tree with `rglob` and content-hashes every indexable
  file on each reindex; the incremental design in [ADR 0016](0016-durable-repo-index.md)
  is built on that being cheap (`vector/chunker.py:158-180`).
- `apply_patch` delegates the actual write to the host's `patch(1)` binary with
  `cwd=root`; path safety is enforced only on the diff's declared headers
  (`tools/apply_patch.py:49`).
- Workspace lifecycle is `git clone` / `shutil.copytree` / `shutil.rmtree` with no
  indirection at all. `workspace_root` is a bare `str` that flows straight into `shutil`
  and `subprocess(cwd=...)` at every consumer (`orchestrator/session.py:55-111`).

Against that, the things that genuinely require the GX10 — the vLLM lanes and the
Qdrant + embed sidecar — **are already HTTP calls over configurable base URLs**.

## Decision

### 1. Backend-as-library: move the process to the files, not the files to the process

The user runs the pipeline on their own machine, against their own checkout. Only
inference and vector traffic crosses the network to the GX10, and both already do.

This is shape 2 of the three, which the roadmap ranked second. The inventory is why it
should be first: every one of the five coupled subsystems above stops being a problem
without a line of remoting, because the code runs where the files are. The alternative
spends its entire budget rebuilding, over an RPC, capabilities that a local process gets
from the operating system for free.

Concretely, nothing in the list needs to change. `run_command` keeps killing its own
process group. `diff_capture` keeps reading untracked bytes. The indexer keeps walking the
tree, and keeps talking to the same remote Qdrant. `apply_patch` keeps shelling out. The
workspace is a directory `shutil` can reach — it is simply the user's directory.

### 2. What actually has to be built is configuration and identity, not transport

The work is not "make the filesystem remote". It is:

- **Point the workspace at an existing checkout instead of creating one.** Today
  `prepare_workspace` either clones a `repo_url` or copies the fixture repo, and always
  owns the directory — including `rmtree`ing it on the way in and on reap. A local-repo
  mode must *adopt* a directory it did not create and must never delete it. That inversion
  is the single most dangerous change in this milestone: the reaper, the workspace LRU and
  `prepare_chained_workspace` all currently assume they may destroy any workspace they can
  see.
- **Decide what a run is allowed to do to a tree the user also uses.** The chained-workspace
  model copies a tree per story; against a real checkout it cannot. The honest options are
  a dedicated git worktree per run, or refusing to start when the tree is dirty. Both
  preserve the deterministic gates; neither is free.
- **Auth.** `require_token` is one shared secret with no identity, no scopes, no expiry,
  and a `?token=` query fallback that can land in access logs. Under this shape the
  blast radius shrinks rather than grows — the local process is the user's own, and the
  GX10 exposes only inference — but the GX10's lane endpoints become the thing reachable
  from more than one machine, and that is where real auth is owed.

### 3. The other two shapes are recorded as rejected, with the reason

**Thin local CLI agent over a WebSocket** (the roadmap's preference). Requires remoting all
five subsystems. The one that makes it unattractive is not the volume of work but
`proc.py`: reproducing "cancel kills the child, including a `subprocess.run` blocked
mid-call" across an RPC means either building an equivalent kill protocol with its own
failure modes, or quietly weakening an invariant the whole cancel story rests on. Trading a
documented guarantee for a deployment convenience is the wrong trade.

**Working-tree sync.** Simplest to state, worst to operate, as the roadmap already said.
Conflict handling would dominate, and it would put a second copy of the tree back in play —
which is the exact thing this milestone exists to remove.

### 4. Not before the console is in real use

Unchanged from the roadmap: Phases A–C are done, but "done" is not "in use". This shape in
particular is a deployment decision, and deployment decisions made before anyone has lived
with the thing tend to encode guesses about how it will be used.

## Consequences

- The console API stops being a service on the GX10 and becomes a local process for
  local-repo mode. [ADR 0011](0011-web-console-off-gx.md) put the console off the GX10;
  this continues that direction rather than reversing it.
- Server-side clone mode does not go away. Both modes are the same code with a different
  workspace origin, which is only true because this shape does not fork the pipeline.
- SQLite stores, attachment blobs and the event log follow the process, so a user running
  locally gets their own history rather than a shared one. For a single-user system that is
  neutral; if the deployment ever becomes multi-machine it is a real decision to revisit.
- **The reaper becomes dangerous and must be made mode-aware before anything else.**
  `reap_console_sessions`, `enforce_workspace_lru` and `prepare_workspace` all `rmtree`
  workspaces today. Pointed at a user's real checkout, any one of them destroys work. This
  is the first thing to build and the first thing to test.
- The Qdrant index keys on the repo's git remote (`vector/scope.py:37-47`), so a local
  checkout of the same repo shares the server-side clone's collection. That is the desired
  behaviour and it comes for free — but it means a local run's overlay must stay an overlay,
  or one machine's uncommitted work becomes visible to another's.
- Nothing here is committed to until M12 starts. This ADR fixes the shape so the next
  session designs within it instead of re-litigating three options.
