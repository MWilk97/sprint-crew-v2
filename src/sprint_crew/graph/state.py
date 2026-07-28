from __future__ import annotations

import operator
from pathlib import Path
from typing import Annotated, Any, TypedDict

from sprint_crew.schemas.change import CodeChange
from sprint_crew.schemas.session import AgentEvent, SessionStatus
from sprint_crew.schemas.ticket import JiraTicket, TaskPlan
from sprint_crew.vector.scope import IndexScope, index_scope_for


class SprintState(TypedDict, total=False):
    session_id: str
    workspace_root: str
    selected_ticket: dict[str, Any]
    task_plan: dict[str, Any]
    code_change: dict[str, Any]
    test_additions: dict[str, Any]
    review_outcome: dict[str, Any]
    attempt: int
    prior_review_feedback: str
    status: SessionStatus
    events: Annotated[list[AgentEvent], operator.add]
    error: str | None
    deadline_epoch: float
    branch: str | None
    pr_url: str | None
    use_real_ship: bool
    workspace_diff: str
    plan_coverage: dict[str, Any]
    baseline_paths: list[str]
    tests_run_this_cycle: bool
    skip_tester_this_attempt: bool
    retry_scope: str
    plan_retries: int
    plan_coverage_prev: dict[str, Any]
    coverage_stall_count: int
    acceptance_failure: dict[str, Any]
    acceptance_test_output: str
    backlog_run_id: str | None
    template_fast_path: bool
    # M7 human review gate. ``review_decisions`` holds one pass's verdicts and is cleared
    # by the rejection retry, so a later attempt never routes on a stale rejection.
    # ``user_rejection_rounds`` is a budget separate from ``attempt``: the human's rounds
    # must not consume MAX_REVIEW_RETRIES.
    review_decisions: list[dict[str, Any]]
    user_rejection_rounds: int
    # Why the run is heading for the failed node, when it is not a review/coverage failure.
    failure_reason: str


def ticket_from_state(state: SprintState) -> JiraTicket:
    return JiraTicket.model_validate(state["selected_ticket"])


def task_plan_from_state(state: SprintState) -> TaskPlan:
    return TaskPlan.model_validate(state["task_plan"])


def code_change_from_state(state: SprintState) -> CodeChange:
    return CodeChange.model_validate(state["code_change"])


def index_scope_from_state(state: SprintState) -> IndexScope:
    """The run's collections: its own overlay in front of the repo's shared index (M8)."""
    return index_scope_for(workspace_from_state(state), run_scope_id=state["session_id"])


def workspace_from_state(state: SprintState) -> Path:
    return Path(state["workspace_root"])
