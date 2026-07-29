from __future__ import annotations

from pathlib import Path

from pydantic_ai.exceptions import ModelAPIError

from sprint_crew.agents.coder import _deadline_reached
from sprint_crew.agents.tech_lead import run_tech_lead
from sprint_crew.agents.tool_events import ToolCallLog
from sprint_crew.config import get_settings
from sprint_crew.orchestrator.acceptance_tests import (
    AcceptanceTestsValidationError,
    validate_acceptance_tests,
)
from sprint_crew.orchestrator.plan_validation import (
    PlanPathValidationError,
    PlanScopeValidationError,
    PlanStructureValidationError,
    validate_plan_paths_exist,
    validate_plan_scope_conflicts,
    validate_plan_structure,
)
from sprint_crew.orchestrator.template_plan import build_template_task_plan_validated
from sprint_crew.schemas.ticket import JiraTicket, TaskPlan


async def run_tech_lead_validated(
    ticket: JiraTicket,
    workspace_root: Path,
    *,
    session_id: str | None = None,
    prior_review_feedback: str = "",
    baseline_paths: frozenset[str] | None = None,
    pre_search_hit_count: int = 0,
    repo_context: str | None = None,
    deadline_epoch: float = 0.0,
) -> tuple[TaskPlan, str, ToolCallLog]:
    """Run TechLead and validate acceptance_tests, structure, and path existence.

    Flow: retry ``run_tech_lead`` up to ``max_attempts`` times, feeding each
    validation failure (acceptance_tests / structure / phantom paths / scope) back
    as targeted feedback; on exhaustion fall back to the deterministic template plan.
    ``repo_context`` (gathered once by the graph node) is reused across every retry
    since the workspace is unchanged within a single planning attempt.

    A lane that times out is an infrastructure failure, not an unplannable ticket, so it
    lands on the same deterministic fallback rather than propagating — and ``deadline_epoch``
    stops the ladder spending its whole budget on a wedged lane.
    """
    settings = get_settings()
    max_attempts = max(3, settings.max_plan_retries + 2)
    feedback = prior_review_feedback
    last_error: Exception | None = None
    planning_mode = "static"
    tool_log: ToolCallLog = []
    baseline = baseline_paths
    infra_failures = 0

    for attempt in range(max_attempts):
        # Checked between attempts only: abandoning a plan mid-flight buys nothing, and
        # the request deadline already bounds the attempt itself.
        if attempt > 0 and _deadline_reached(deadline_epoch):
            break
        try:
            plan, planning_mode = await run_tech_lead(
                ticket,
                workspace_root,
                session_id=session_id,
                prior_review_feedback=feedback,
                tool_call_log=tool_log,
                pre_search_hit_count=pre_search_hit_count,
                repo_context=repo_context,
            )
        except ModelAPIError as exc:
            # The lane failed, not the plan. The SDK has already retried inside its own
            # deadline, so allow one more pass for a genuinely transient blip and then take
            # the deterministic plan — spending the rest of the ladder on a wedged lane is
            # what cost a 3-story trap run 1h53m in this node.
            last_error = exc
            infra_failures += 1
            if infra_failures > 1:
                break
            continue
        if baseline is None:
            from sprint_crew.orchestrator.plan_validation import snapshot_baseline_paths

            baseline = snapshot_baseline_paths(workspace_root)
        try:
            validate_acceptance_tests(plan.acceptance_tests)
            validate_plan_structure(
                plan,
                ticket,
                workspace_root=workspace_root,
                baseline_paths=baseline,
            )
            validate_plan_scope_conflicts(plan)
            if planning_mode not in {"template", "template_fallback"}:
                validate_plan_paths_exist(workspace_root, plan, baseline_paths=baseline)
            return plan, planning_mode, tool_log
        except AcceptanceTestsValidationError as exc:
            last_error = exc
            if attempt < max_attempts - 1:
                feedback = (
                    f"{prior_review_feedback}\n\n".strip()
                    + "TaskPlan validation failed: "
                    + str(exc)
                    + ". acceptance_tests must be allowlisted shell commands only "
                    "(e.g. pytest -q), not prose from acceptance_criteria."
                ).strip()
        except PlanStructureValidationError as exc:
            last_error = exc
            if attempt < max_attempts - 1:
                extra = ""
                if "coverage missing" in feedback.lower():
                    extra = (
                        " If listed missing paths already exist unchanged, "
                        "remove them from files_to_touch instead of re-planning edits."
                    )
                feedback = (
                    f"{prior_review_feedback}\n\n".strip()
                    + "TaskPlan structure validation failed: "
                    + str(exc)
                    + ". Ensure files_to_touch lists every file from all steps only."
                    + extra
                ).strip()
        except PlanPathValidationError as exc:
            last_error = exc
            if attempt < max_attempts - 1:
                feedback = (
                    f"{prior_review_feedback}\n\n".strip()
                    + "TaskPlan path validation failed: "
                    + ", ".join(exc.phantom_paths)
                    + " do not exist. Use list_directory and semantic_search. "
                    "Only plan paths that exist or are new tests under tests/."
                ).strip()
        except PlanScopeValidationError as exc:
            last_error = exc
            if attempt < max_attempts - 1:
                feedback = (
                    f"{prior_review_feedback}\n\n".strip()
                    + "TaskPlan scope validation failed: "
                    + ", ".join(exc.conflicts)
                    + " appear in both files_to_touch/steps and out_of_scope. "
                    "Remove them from out_of_scope or drop them from the plan."
                ).strip()

    try:
        plan = build_template_task_plan_validated(ticket)
        if baseline is None:
            from sprint_crew.orchestrator.plan_validation import snapshot_baseline_paths

            baseline = snapshot_baseline_paths(workspace_root)
        validate_acceptance_tests(plan.acceptance_tests)
        validate_plan_structure(
            plan,
            ticket,
            workspace_root=workspace_root,
            baseline_paths=baseline,
        )
        return plan, "template_fallback", tool_log
    except (
        AcceptanceTestsValidationError,
        PlanStructureValidationError,
        PlanPathValidationError,
        PlanScopeValidationError,
        RuntimeError,
    ):
        pass

    assert last_error is not None
    raise RuntimeError(str(last_error)) from last_error
