# ADR 0013: LLM-generated clarify and the Interpreter as multimodal boundary

## Status

Accepted — Phase 1 implemented (LLM clarify with recommendations). Attachments (files,
images) are designed here but not yet built; see Consequences.

## Context

[ADR 0012](0012-plan-code-modes-and-clarify.md) established that no sprint run starts from
a raw prompt: a clarify step and an explicit confirmation come first. The MVP shipped that
state machine with a **deterministic stub** — two or three fixed questions, lightly keyed off
words in the prompt. It proves the flow but does not read the request, so it asks the same
things whether the user wants a one-line docstring or a new subsystem, and it never has an
opinion about the answer.

Users also want to bring files and images into the request (a mockup, a stack trace, a
spec). The pipeline is text-only end to end, and the Work lane model was text-only too.

## Decision

**1. Clarify questions come from a model.** A new **Interpreter** role runs on the Work lane
before planning and returns an `IntentAnalysis`: a restatement of the goal, its assumptions,
the remaining unknowns, and the questions worth interrupting the user for. Asking nothing is
an explicit, valid outcome — a clear request goes straight to `ready`.

**2. Every question carries a recommendation.** Each question has a `recommended_suggestion_id`
and a per-option `rationale`. A user who reads only the recommended option and accepts must
get sensible work; this is what makes the step feel like a collaborator rather than a form.

**3. The model does not invent identifiers.** It returns ordered questions and a
`recommended_index`; Python assigns `question_id` / `suggestion_id` and clamps the index.
Consistent with the existing split in [agent-orchestration](../agent-orchestration.md):
deterministic checks stay in Python.

**4. The Interpreter is the only multimodal role.** The Work lane runs
`RedHatAI/Qwen3.6-35B-A3B-NVFP4`, which is natively multimodal. Images are sent **only** by
the Interpreter, which converts them into text (the restated goal, assumptions, and derived
context). ScrumMaster, TechLead, Coder, Tester, and Reviewer stay text-only forever.

**5. Clarify degrades, it never blocks.** If the Work lane is cold or the call fails, the
deterministic stub answers instead. An interactive caller must not wait out a model load;
`CLARIFY_AUTOSTART_LANE=true` opts into waiting when latency does not matter.

## Consequences

- The Work lane model changed from `Qwen3-30B-A3B-Thinking-2507` to `Qwen3.6-35B-A3B-NVFP4`,
  which also moved that lane onto the GB10 vLLM image (transformers 5.x) and changed its
  tool-call parser from `hermes` to `qwen3_coder`. TechLead's `tool_loop` depends on that
  parser — see [model-evaluation](../model-evaluation.md) for the probe results.
- **No third lane.** Interpreter is a role on the existing Work lane, so the one-lane-at-a-time
  memory policy ([AGENTS.md 4.1](../../AGENTS.md)) is unchanged. Clarify runs in the pre-run
  phase where the Work lane is loaded for ScrumMaster anyway, so it costs no extra lane swap.
- `ensure_lane` now stops other lanes by **lane name** rather than by `Role`. `_ROLE_TO_LANE`
  is many-to-one, so a second role pointing at the work lane would previously have stopped
  the container it was about to start. A no-op today, and a dedicated Interpreter role is
  safe to add later.
- Clarify quality is now non-deterministic. The stub remains in the codebase as the fallback
  path and keeps its tests.
- Attachments are **not implemented**. When they are, attachment content is untrusted input:
  it must be fenced and marked as data, never instructions, because this system opens PRs.
