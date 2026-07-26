"""Coverage-round and build-fix retry ladder wrapped around the Coder tool loop.

Mirrors the tech_lead.py / tech_lead_planning.py split: coder.py owns the raw loop and
step-mode iteration, this module owns the "keep going until plan coverage is satisfied,
then verify with acceptance tests" policy on top of it.
"""

from __future__ import annotations

from pathlib import Path

from sprint_crew.agents.coder import (
    _continuation_turn_budget,
    _deadline_reached,
    run_coder_loop,
    run_coder_plan,
)
from sprint_crew.agents.prompts_coder import (
    build_coder_build_fix_prompt,
    build_coder_continuation_prompt,
)
from sprint_crew.config import get_settings
from sprint_crew.orchestrator.acceptance_failure import analyze_acceptance_output
from sprint_crew.orchestrator.acceptance_tests import run_acceptance_tests
from sprint_crew.orchestrator.plan_coverage import (
    PlanCoverageResult,
    continuation_makes_sense,
    coverage_improved,
    validate_plan_coverage,
)
from sprint_crew.orchestrator.run_registry import cancel_requested
from sprint_crew.schemas.ticket import TaskPlan


async def run_coder_with_coverage(
    task_plan: TaskPlan,
    workspace_root: Path,
    *,
    role_specialization: str | None = None,
    prior_review_feedback: str = "",
    baseline_paths: frozenset[str] | None = None,
    deadline_epoch: float = 0.0,
    attempt: int = 0,
) -> tuple[str, list[dict], PlanCoverageResult, str, bool]:
    """Run step-aware Coder, then continuation rounds until coverage satisfied or cap hit."""
    settings = get_settings()
    # One shared log for every loop this node runs, so tool-call ``index`` is continuous
    # across steps and continuation rounds (see run_coder_loop). The callees append into
    # it and return it, so the returned value is discarded rather than merged.
    tool_log: list[dict] = []
    raw_output, _ = await run_coder_plan(
        task_plan,
        workspace_root,
        role_specialization=role_specialization,
        prior_review_feedback=prior_review_feedback,
        baseline_paths=baseline_paths,
        deadline_epoch=deadline_epoch,
        attempt=attempt,
        tool_call_log=tool_log,
    )

    coverage = validate_plan_coverage(
        task_plan,
        workspace_root,
        baseline_paths=baseline_paths,
    )
    continuation_budget = _continuation_turn_budget()
    prior_coverage = coverage
    for _ in range(settings.max_coverage_rounds):
        if coverage.satisfied:
            break
        if _deadline_reached(deadline_epoch) or cancel_requested():
            break
        if not continuation_makes_sense(coverage, workspace_root, task_plan):
            break
        continuation_prompt = build_coder_continuation_prompt(coverage)
        continuation_output, _ = await run_coder_loop(
            task_plan,
            workspace_root,
            role_specialization=role_specialization,
            user_prompt=continuation_prompt,
            max_turns=continuation_budget,
            baseline_paths=baseline_paths,
            deadline_epoch=deadline_epoch,
            attempt=attempt,
            tool_call_log=tool_log,
        )
        raw_output = continuation_output
        new_coverage = validate_plan_coverage(
            task_plan,
            workspace_root,
            baseline_paths=baseline_paths,
        )
        if not coverage_improved(prior_coverage, new_coverage):
            coverage = new_coverage
            break
        prior_coverage = coverage
        coverage = new_coverage

    acceptance_output = ""
    acceptance_verified = False
    if coverage.satisfied and not _deadline_reached(deadline_epoch) and not cancel_requested():
        # Cancel is checked before starting: launching a 900 s child after Stop is what
        # makes a cancel feel broken. Once running, the child dies with the task
        # (sprint_crew.proc) rather than outliving the run that spawned it.
        acceptance_output, acceptance_green = await run_acceptance_tests(
            workspace_root, task_plan.acceptance_tests
        )
        acceptance_verified = acceptance_green
        if not acceptance_green and acceptance_output.strip():
            analysis = analyze_acceptance_output(acceptance_output)
            if analysis.kind != "none" and not analysis.tester_can_help:
                build_fix_prompt = build_coder_build_fix_prompt(analysis)
                continuation_output, _ = await run_coder_loop(
                    task_plan,
                    workspace_root,
                    role_specialization=role_specialization,
                    user_prompt=build_fix_prompt,
                    max_turns=continuation_budget,
                    baseline_paths=baseline_paths,
                    deadline_epoch=deadline_epoch,
                    attempt=attempt,
                    tool_call_log=tool_log,
                )
                raw_output = continuation_output

    return raw_output, tool_log, coverage, acceptance_output, acceptance_verified
