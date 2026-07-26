from __future__ import annotations

import asyncio
from pathlib import Path

from sprint_crew.agents.prompts_reviewer import (
    build_reviewer_system_prompt,
    build_reviewer_user_prompt,
)
from sprint_crew.config import Role
from sprint_crew.inference.structured import structured_completion
from sprint_crew.orchestrator.acceptance_tests import run_acceptance_tests
from sprint_crew.orchestrator.workspace_diff import gather_workspace_diff
from sprint_crew.schemas.change import CodeChange, ReviewOutcome
from sprint_crew.schemas.ticket import TaskPlan

_DIFF_MAX_CHARS = 24_000


async def run_reviewer(
    task_plan: TaskPlan,
    code_change: CodeChange,
    workspace_root: Path,
    *,
    workspace_diff: str = "",
    test_additions_json: str = "",
    ticket_acceptance_criteria: str = "",
    tests_already_run: bool = False,
    coverage_summary: str = "",
    files_to_touch: list[str] | None = None,
) -> ReviewOutcome:
    root = workspace_root.resolve()
    if tests_already_run and code_change.tests_passed:
        test_results = (
            "Acceptance tests were already run earlier in this cycle and passed. "
            "Trust exit_code=0 from the prior run."
        )
        tests_passed = True
    else:
        # to_thread: run_acceptance_tests is a blocking subprocess.run bounded by
        # ACCEPTANCE_TEST_TIMEOUT_S (900 s). Held on the event loop it stalls the whole
        # process — no SSE frames, no /health, and POST /cancel cannot even be accepted,
        # which is what the cancel contract in api/console.py assumes stays responsive.
        test_results, tests_passed = await asyncio.to_thread(
            run_acceptance_tests,
            root,
            task_plan.acceptance_tests,
        )

    diff = workspace_diff.strip() or gather_workspace_diff(
        root,
        max_chars=_DIFF_MAX_CHARS,
        priority_paths=files_to_touch or task_plan.files_to_touch,
    )

    # to_thread: structured_completion is a blocking OpenAI SDK call; run off the event
    # loop so SSE frames and /health can flush during a long Reviewer turn (M3).
    review = await asyncio.to_thread(
        structured_completion,
        Role.WORK,
        system_prompt=build_reviewer_system_prompt(),
        user_prompt=build_reviewer_user_prompt(
            task_plan_json=task_plan.model_dump_json(indent=2),
            code_change_json=code_change.model_dump_json(indent=2),
            git_diff=diff,
            test_results=test_results,
            ticket_acceptance_criteria=ticket_acceptance_criteria,
            test_additions_json=test_additions_json,
            coverage_summary=coverage_summary,
            files_to_touch=files_to_touch or task_plan.files_to_touch,
        ),
        output_type=ReviewOutcome,
        timeout_seconds=600,
        max_retries=3,
    )

    updates: dict[str, object] = {}
    if not review.ticket_key:
        updates["ticket_key"] = task_plan.ticket_key
    if review.tests_passed != tests_passed:
        updates["tests_passed"] = tests_passed
    if not review.tests_run:
        updates["tests_run"] = list(task_plan.acceptance_tests)
    blockers = [f for f in review.findings if f.severity == "blocker"]
    if blockers or not tests_passed:
        updates["passed"] = False
    elif tests_passed and not blockers:
        # Ground truth: green tests + no blockers => pass (warnings/nits alone do not block).
        # Thinking models sometimes emit passed=false without findings despite green tests.
        updates["passed"] = True
    if not review.passed and review.retry_scope not in ("code", "plan"):
        updates["retry_scope"] = "code"

    return review.model_copy(update=updates) if updates else review
