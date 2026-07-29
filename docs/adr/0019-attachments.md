# ADR 0019 — Attachments, and the boundary that keeps them out of a run

**Status:** Accepted (2026-07-29) · **Milestone:** M11 · **Builds on:** [0013](0013-interpreter-clarify.md) (Interpreter is the only multimodal role), [0016](0016-durable-repo-index.md) (session-owned checkout), [0017](0017-codebase-chat.md) (fencing repo content as data)

## Context

ADR 0013 designed attachments and did not build them: the Interpreter would be the only
role to receive images, and attachment content would be "fenced and marked as data, never
instructions, because this system opens PRs". Two years of milestones later the pieces it
assumed are all present — a session that owns a checkout, a durable store, an event
timeline — and nothing had been uploaded to any of them.

The roadmap flagged one blocker: `AGENTS.md` §8.4 and ADR 0013 were said to disagree about
whether the deployed Work model has a vision tower, and M11 could not be planned until that
was settled. They do not disagree. `infra/docker-compose.yml` deploys
`RedHatAI/Qwen3.6-35B-A3B-NVFP4` and deliberately omits `--language-model-only` with a
comment citing this ADR's predecessor; `infra/models.yaml` sets `supports_vision: true`;
`LaneConfig.supports_vision` has been a typed field all along. The blocker was stale.

What actually needed deciding was smaller and more practical: where bytes live, what is
allowed in, and what stops an attachment reaching the part of the system that writes code.

## Decision

### 1. Blobs live outside the checkout, addressed by content

Uploads go to `CONSOLE_ATTACHMENT_BASE` (`~/.sprint-crew/attachments/{session}/{sha256}`),
a new root that is not under `workspace_base`.

This is not tidiness. Since M8 `workspace_base/{session_id}` *is* the session's git
checkout. An upload written anywhere inside it would appear in `git status --porcelain` —
which `plan_coverage.collect_changed_paths` reads to decide whether the agent touched what
the plan said — and in the indexer's file walk. A screenshot must not be able to look like
a change the agent made, or to be indexed as source.

Metadata goes in SQLite next to the other stores, following `DiffStore` rather than
`SqliteJsonStore`: a payload column is the wrong home for megabytes. Blobs are named by
sha256, so pasting one screenshot twice costs one blob and resolves to one attachment id —
which also makes a retried upload idempotent rather than a way to burn the session's quota.

### 2. Two checks on the way in, because a declared type is a claim

Every upload is checked twice. The declared media type decides whether it is allowed at all
against a small allowlist; then the bytes decide what it really is. An image is sniffed by
signature and must match what was declared, and a declared text type must decode as UTF-8
with no NUL bytes.

A file announcing `image/png` that is not a PNG is a 415 before it reaches a vision tower.
This is the cheapest place in the whole system to stop a hostile upload, and the only one
where the check is a dozen lines.

The allowlist is deliberately small — four image types and six text types. The Interpreter
can do exactly two things with an attachment: look at it, or read it. There is no third
`AttachmentKind` member for the same reason: an upload the allowlist cannot place in one of
those two is refused rather than carried as an unknown.

### 3. Only derived text crosses into a run

This is the load-bearing decision and the reason ADR 0013 wrote its requirement in bold.

An attachment reaches exactly one model: the Interpreter, at clarify time. What the
Interpreter produces is an `IntentAnalysis` — restated goal, assumptions, questions — and
*that* is what flows onward into `build_run_prompt` and from there to the ScrumMaster,
TechLead, Coder, Tester and Reviewer. The bytes never do. A prompt injected into a
screenshot has to survive being read by a model whose output is a structured set of
clarify questions that a human then answers, which is a materially different thing from
appearing verbatim in a Coder's prompt.

Three structural tests enforce it, all grep-shaped like the event-vocabulary test that
already guards `EventType`: only `prompts_interpreter.py` among the prompt modules mentions
`AttachmentPayload`, only `interpreter.py` among the agent modules does, and `read_blob` has
exactly three call sites, none of them an agent. A fourth asserts the marker text of an
uploaded log does not appear in `build_run_prompt`'s output.

The behavioural half — whether the model resists an instruction it is shown — is not
testable without a GPU, and the suite is unit-only by design. `probe_interpreter.py --image`
is where that is measured.

### 4. The fence is explicit, and the system prompt says why it matters

Attachment content is wrapped in `<<<BEGIN ATTACHMENT {name} ({type})>>>` … `<<<END
ATTACHMENT>>>` and preceded by a line naming it untrusted. The Interpreter's system prompt
gained a paragraph — it had none before, while `prompts_explainer.py` had carried the
repo-content equivalent since M9 — that names the failure mode concretely ("ignore your
instructions", "push to main") and states the stake: what the Interpreter writes becomes
the brief for agents that open pull requests.

Images are announced by name in the fenced block as well as being sent as content parts. A
bare content part arrives anonymous, and a model given three pictures and asked about "the
screenshot" cannot say which one it means.

### 5. Images ride the existing structured call, and degrade when they cannot

`structured_completion`'s `user_prompt` widens from `str` to `str | list[dict]`. A list
already flowed through untouched — `content` was assigned directly — but an accidental
capability is not a contract, so the annotation is now explicit and documented as
Interpreter-only.

Whether an image survives `response_format: json_schema, strict: true` on this lane was
never exercised: the existing vision probe uses a raw client and free text, which answers a
different question. `probe_interpreter.py --image` now runs both.

**Measured 2026-07-29 on the GX10, `qwen3.6-35b-a3b-nvfp4`: it works.** The raw vision
round-trip returned in 1.3 s, and the same image through `run_interpreter` — fenced block,
content parts, `IntentAnalysis` schema, `strict: true` — returned valid structured output in
26.6 s having correctly read the picture ("three colored blocks (red, green, blue)"). No
fallback is needed. Had it failed, the design would have been a free-text describe call
feeding the normal text-only `IntentAnalysis`, which is worth remembering as the escape
hatch if a future lane swap breaks the combination.

26.6 s against 24.8 s for the text-only vague probe: on this lane an image is close to free
next to the reasoning the Interpreter was already doing.

When the lane has no vision tower — the documented `qwen3-30b-a3b-thinking` rollback —
image data is dropped and the attachment stays announced by name. Clarify degrades, never
blocks (ADR 0013); an attached screenshot becomes something the Interpreter knows it was
given and cannot see, rather than a failed request.

## Consequences

- `CreateConsoleSessionRequest` does **not** gain `attachment_ids`, contrary to the
  roadmap. Uploads are session-scoped, so nothing could populate it; a client pasting into
  an empty composer creates the session first, which is one fast round-trip.
- Only the newest user turn's attachments are sent. Earlier ones were already interpreted
  and survive as `prior_clarifications`; re-sending every image each round would multiply a
  long conversation's cost by its own history.
- Uploads stay allowed while a run holds the lane. Storing bytes needs no model, and
  `POST /messages` already 409s once a run has started — duplicating that gate here would
  only stop a user staging a screenshot for the message they intend to send next.
- Disk grows in a second dimension. `CONSOLE_ATTACHMENT_MAX_BYTES` (5 MB) and
  `CONSOLE_MAX_ATTACHMENTS_PER_SESSION` (20) bound it per session, and both the TTL reaper
  and user-triggered purge delete the session's blob directory. The workspace LRU does
  not touch attachments — an evicted clone is recoverable by re-cloning, an evicted upload
  is gone.
- `/health` still reports only workspace disk. Attachments now consume a root it does not
  count, which is worth adding the next time that endpoint is touched.
- Text attachments are excerpted head-and-tail at `CONSOLE_ATTACHMENT_EXCERPT_BYTES`. A
  log's head carries the command and its tail carries the traceback, and taking only one
  end is how an excerpt looks complete while dropping the reason it was attached.
