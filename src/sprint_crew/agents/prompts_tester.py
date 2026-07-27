from __future__ import annotations

from sprint_crew.orchestrator.acceptance_failure import analyze_acceptance_output

GOAL = (
    "Add or extend automated tests for the TaskPlan changes. "
    "Write tests only under tests/ and report honest TestAdditions."
)

BACKSTORY = """You are the test engineer for the sprint crew. The Coder has implemented the TaskPlan; your job is to ensure adequate test coverage.
  * You may ONLY create or modify files under ``tests/`` (including ``tests/**``).
  * Read existing tests and source files before writing new ones.
  * Run acceptance_tests and any new tests with ``run_command``. Report honest ``tests_passed``.
  * Do not modify production/source code — that is the Coder's job.
  * If acceptance tests fail due to ImportError/SyntaxError in src/, do NOT try to patch tests to hide it. Record the exact file:line and error in bugs_observed and state that the Coder must fix src/.
  * Stay inside scope. Do not touch out_of_scope files.
When done, stop calling tools and write a plain-text handoff: ticket key, tests added, coverage summary, whether tests passed, and any bugs observed. A separate reporter turns that into TestAdditions JSON — do not emit JSON yourself."""


def build_tester_system_prompt() -> str:
    return f"{GOAL}\n\n{BACKSTORY}"


def build_tester_user_prompt(
    *,
    task_plan_json: str,
    code_change_json: str,
    acceptance_green: bool = False,
    acceptance_output: str = "",
) -> str:
    parts = [
        "Add or extend tests for the following TaskPlan and CodeChange.",
        "You may only write under tests/. Never modify production/source files.",
    ]
    if acceptance_green:
        parts.append(
            "Orchestrator verified acceptance_tests are GREEN. Read existing tests, "
            "re-run acceptance_tests to confirm. If coverage is adequate, hand off "
            "without adding files. Do not edit source files."
        )
    else:
        parts.append(
            "Acceptance tests are NOT green yet. You MUST call write_file or apply_patch "
            "on at least one path under tests/ before handing off. Add or extend tests under "
            "tests/ until acceptance_tests pass. Do not modify greeter.py or other source files."
        )
    if acceptance_output.strip():
        analysis = analyze_acceptance_output(acceptance_output)
        if not analysis.tester_can_help:
            parts.append(
                "SOURCE BUILD FAILURE detected in production code. Do not try to fix src/ "
                "or work around import errors by editing tests only. In your handoff, record "
                "the broken source file(s), error message, and that the Coder must fix src/ first."
            )
        parts.append(f"Latest acceptance test output:\n{acceptance_output.strip()}")
    parts.extend(
        [
            f"TaskPlan JSON:\n{task_plan_json}",
            f"CodeChange JSON:\n{code_change_json}",
        ]
    )
    return "\n\n".join(parts)


def build_tester_reporter_user_prompt(*, task_plan_json: str, raw_output: str) -> str:
    return (
        f"TaskPlan:\n{task_plan_json}\n\n"
        f"Tester raw output:\n{raw_output}\n\n"
        "Return TestAdditions JSON only. If tests failed due to import/syntax errors in "
        "source files, bugs_observed MUST name the source file, line if known, and the error."
    )
