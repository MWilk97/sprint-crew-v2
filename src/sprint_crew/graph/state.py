from __future__ import annotations

import operator
from pathlib import Path
from typing import Annotated, Any, TypedDict

from sprint_crew.schemas.change import CodeChange
from sprint_crew.schemas.session import AgentEvent, SessionStatus
from sprint_crew.schemas.ticket import JiraTicket, TaskPlan


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


def ticket_from_state(state: SprintState) -> JiraTicket:
    return JiraTicket.model_validate(state["selected_ticket"])


def task_plan_from_state(state: SprintState) -> TaskPlan:
    return TaskPlan.model_validate(state["task_plan"])


def code_change_from_state(state: SprintState) -> CodeChange:
    return CodeChange.model_validate(state["code_change"])


def workspace_from_state(state: SprintState) -> Path:
    return Path(state["workspace_root"])
