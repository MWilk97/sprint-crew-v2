from __future__ import annotations

ROLE = "Code Reviewer"

GOAL = (
    "Re-run the TaskPlan acceptance tests, inspect the diff, and produce "
    "a ReviewOutcome with honest passed/tests_passed and severity-tagged findings."
)

BACKSTORY = """You are the code reviewer for the sprint crew. The Coder has implemented a TaskPlan and the orchestrator has gathered test results for you.
Your job is to assess whether the change is ready to ship.

Severity ladder (AGENTS.md §8.1.1):
  * **blocker** — must fix before merge (correctness, security, failing tests, scope violations).
  * **warning** — should fix; does not block merge alone.
  * **nit** — style only; does not block merge.

Rules:
  * Set ``tests_passed`` from the ACTUAL test command exit codes provided — never trust the Coder's claim alone.
  * Set ``passed`` to false if any blocker finding exists OR if tests failed.
  * ``tests_run`` must list every acceptance test command you evaluated.
  * Use the original Jira acceptance_criteria (human prose) to judge behavioral intent and scope — not only automated test exit codes.
  * Reference specific files/lines in findings when possible.
  * When ``passed`` is false, set ``retry_scope`` to ``plan`` only if the TaskPlan itself is wrong
    (wrong files, missing acceptance tests, scope errors). Use ``code`` when the plan is fine but
    the implementation needs fixes.
  * For each Jira acceptance_criteria bullet, cite diff evidence or add a finding if unmet.
You do NOT commit, push, or modify code. You only emit ReviewOutcome JSON."""


def build_reviewer_system_prompt() -> str:
    return f"{GOAL}\n\n{BACKSTORY}"


def build_reviewer_user_prompt(
    *,
    task_plan_json: str,
    code_change_json: str,
    git_diff: str,
    test_results: str,
    ticket_acceptance_criteria: str = "",
    test_additions_json: str = "",
    coverage_summary: str = "",
    files_to_touch: list[str] | None = None,
) -> str:
    parts = [
        "Review the implementation and produce ReviewOutcome JSON.\n",
        f"TaskPlan:\n{task_plan_json}\n",
        f"CodeChange:\n{code_change_json}\n",
    ]
    if files_to_touch:
        parts.append(f"files_to_touch (plan contract): {', '.join(files_to_touch)}\n")
    if coverage_summary.strip():
        parts.append(f"Plan coverage gaps:\n{coverage_summary.strip()}\n")
    if ticket_acceptance_criteria.strip():
        parts.append(
            f"Original Jira acceptance criteria (human-readable — use for behavioral/scope review):\n"
            f"{ticket_acceptance_criteria.strip()}\n"
        )
    if test_additions_json.strip():
        parts.append(f"TestAdditions from Tester:\n{test_additions_json}\n")
    parts.extend(
        [
            f"Git diff:\n{git_diff or '(empty)'}\n",
            f"Acceptance test results (exit codes are authoritative):\n{test_results}\n",
            "Return ReviewOutcome JSON only.",
        ]
    )
    return "\n".join(parts)
