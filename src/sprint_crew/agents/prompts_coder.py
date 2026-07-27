from __future__ import annotations

from sprint_crew.orchestrator.acceptance_failure import AcceptanceFailureAnalysis
from sprint_crew.orchestrator.plan_coverage import PlanCoverageResult
from sprint_crew.schemas.ticket import PlanStep

_SPECIALIZATION_PREFIX = (
    "You operate as a {specialization}. The tech-stack hint above "
    "is AUTHORITATIVE for THIS sprint cycle: write idiomatic code "
    "in the language and framework the role implies "
    "(matching the existing project conventions when the workspace "
    "is in that stack), pick libraries / build tools / test "
    "runners that the role's community treats as defaults, and "
    "phrase any inline rationale / commit body / CodeChange.notes "
    "in language a peer at that seniority would write. The general "
    "Coder rules below still apply -- the specialization narrows "
    "*how* you implement, not *whether* you honour the work-tree "
    "safety / honest-test-reporting / scope-discipline contract."
    "\n\n"
)

GOAL = (
    "Execute the TaskPlan: edit the files it names, run the acceptance "
    "tests it lists, and produce a CodeChange that honestly reports "
    "what was written and whether the tests passed when you ran them."
)

BACKSTORY = """You are the implementation agent for the sprint crew. The TechLead has handed you a TaskPlan; your job is to execute it step by step. Concretely:
  * Read the files the plan names before editing them. Make targeted edits with ``apply_patch`` when you can, and use ``write_file`` when a whole file needs to be replaced or a new file added.
  * Run the plan's acceptance_tests with ``run_command``. The output is rolled up in ``CodeChange.tests_passed`` -- be HONEST. The Reviewer re-runs the same commands and will catch any lie immediately.
  * Write or edit files under ``tests/`` ONLY when the TaskPlan lists them in ``steps[].files`` or ``files_to_touch``. Otherwise implement source files and run ``acceptance_tests``; do not duplicate work reserved for the Test Engineer.
  * When the TaskPlan introduces new behaviour and lists test files in the plan, add or extend tests in those paths. Conventional locations when the plan names them: Python -> ``tests/test_<module>.py``; JS/TS -> ``<module>.test.ts`` or ``__tests__/<module>.test.ts``; Java -> ``src/test/.../<Class>Test.java``; Go -> ``<file>_test.go``. Docs-only / dependency-bump tickets are exempt.
  * Stay inside the workspace. Every path you touch goes through path safety rules. Don't try to escape with '..'.
  * Stay inside scope. The plan lists ``out_of_scope`` items explicitly; do not touch them even if they look adjacent. ``write_file`` and ``apply_patch`` are rejected at the tool layer for paths outside ``files_to_touch``/``steps`` or inside ``out_of_scope``.
  * Code style -- comments. Do NOT add comments that just narrate what the code already says. Write a comment ONLY when the next reader cannot derive intent from the code itself.
You DO NOT commit, push, open a PR, comment on Jira, or transition tickets. Those are deterministic Python side effects driven by the orchestrator after the Reviewer signs off.
When the diff is ready and the tests are green (or you have hit the iteration ceiling and want to hand off honestly), stop calling tools and write a plain-text handoff: ticket key, branch name, summary, files touched, whether acceptance tests passed when you ran them, and any notes. A separate Formatter agent turns that into CodeChange JSON — do not emit JSON yourself."""


def build_coder_system_prompt(*, role_specialization: str | None = None) -> str:
    spec = (role_specialization or "").strip()
    body = (_SPECIALIZATION_PREFIX.format(specialization=spec) + BACKSTORY) if spec else BACKSTORY
    return f"{GOAL}\n\n{body}"


def build_coder_user_prompt(task_plan_json: str, *, prior_review_feedback: str = "") -> str:
    parts = [
        "Implement the following TaskPlan in the workspace. "
        "Use tools to read, edit, and run acceptance tests.",
        f"TaskPlan JSON:\n{task_plan_json}",
    ]
    if prior_review_feedback.strip():
        parts.insert(
            1,
            "Prior review feedback (fix these blockers before proceeding):\n"
            f"{prior_review_feedback.strip()}",
        )
    return "\n\n".join(parts)


def build_coder_step_prompt(
    *,
    task_plan_json: str,
    step_index: int,
    step: PlanStep,
    total_steps: int,
    workspace_diff: str = "",
    prior_review_feedback: str = "",
) -> str:
    files = ", ".join(step.files) if step.files else "(see step description)"
    parts = [
        f"Implement ONLY step {step_index + 1}/{total_steps} of the TaskPlan.",
        f"Step description: {step.description}",
        f"Files for this step: {files}",
        "Do not skip ahead to later steps. Complete this step before stopping.",
        "Run relevant tests if this step introduces testable behavior.",
        f"TaskPlan JSON:\n{task_plan_json}",
    ]
    if workspace_diff.strip():
        parts.insert(
            4,
            f"Workspace diff so far (truncated):\n{workspace_diff.strip()}",
        )
    if prior_review_feedback.strip():
        parts.insert(
            1,
            "Prior review feedback (fix these blockers before proceeding):\n"
            f"{prior_review_feedback.strip()}",
        )
    return "\n\n".join(parts)


def build_coder_continuation_prompt(coverage: PlanCoverageResult) -> str:
    parts = [
        "Plan coverage is incomplete. Finish the TaskPlan before handing off.",
    ]
    if coverage.missing:
        parts.append("Missing files (must create or modify):\n- " + "\n- ".join(coverage.missing))
    if coverage.unexpected:
        parts.append(
            "Unexpected files changed (revert or justify in handoff):\n- "
            + "\n- ".join(coverage.unexpected)
        )
    if coverage.out_of_scope_hits:
        parts.append(
            "Out-of-scope files touched (revert immediately):\n- "
            + "\n- ".join(coverage.out_of_scope_hits)
        )
    parts.append("Use tools to complete missing work, then run acceptance tests.")
    return "\n\n".join(parts)


def build_coder_build_fix_prompt(analysis: AcceptanceFailureAnalysis) -> str:
    parts = [
        "Acceptance tests failed before assertions could run because source code is broken.",
        analysis.summary,
    ]
    if analysis.source_paths:
        parts.append("Fix these source files first:\n- " + "\n- ".join(analysis.source_paths))
    parts.append(
        "The Tester agent cannot edit src/. Repair imports, syntax, or missing modules "
        "in source files, then re-run acceptance_tests."
    )
    if analysis.detail_excerpt.strip():
        parts.append(f"Error excerpt:\n{analysis.detail_excerpt.strip()}")
    parts.append("Do not add workaround paths like /testbed or /workspace.")
    return "\n\n".join(parts)
