from __future__ import annotations

import re

from sprint_crew.agents.tech_lead import PlanStructureValidationError, validate_plan_structure
from sprint_crew.orchestrator.acceptance_tests import (
    AcceptanceTestsValidationError,
    validate_acceptance_tests,
)
from sprint_crew.orchestrator.complexity import _paths_in_text
from sprint_crew.schemas.ticket import JiraTicket, PlanStep, TaskPlan

_MODULE_RE = re.compile(r"\b(\w+)\s+module\b", re.IGNORECASE)


def _infer_module_paths(text: str) -> list[str]:
    paths: list[str] = []
    for match in _MODULE_RE.finditer(text):
        name = match.group(1).lower()
        if name not in {"the", "a", "this"}:
            paths.append(f"{name}.py")
    return paths


def _expand_paths_with_tests(paths: list[str]) -> list[str]:
    expanded = list(paths)
    seen = set(paths)
    for path in paths:
        if path.startswith("tests/") or not path.endswith(".py"):
            continue
        stem = path.rsplit("/", 1)[-1].removesuffix(".py")
        test_path = f"tests/test_{stem}.py"
        if test_path not in seen:
            seen.add(test_path)
            expanded.append(test_path)
    return expanded


_PYTEST_CMD_RE = re.compile(r"pytest\s+[\w./-]+(?:\s+-[a-zA-Z]+(?:\s+[\w./-]+)?)?", re.IGNORECASE)


def _ticket_text(ticket: JiraTicket) -> str:
    return "\n".join(
        [
            ticket.summary,
            ticket.description,
            ticket.acceptance_criteria,
        ]
    ).strip()


def _acceptance_tests_from_ticket(ticket: JiraTicket, paths: list[str]) -> list[str]:
    text = _ticket_text(ticket)
    test_paths = [p for p in paths if p.startswith("tests/") or p.endswith("_test.py")]
    if test_paths:
        return [f"pytest {test_paths[0]} -q"]

    match = _PYTEST_CMD_RE.search(text)
    if match:
        return [match.group(0).strip()]

    if "pytest" in text.lower():
        return ["pytest -q"]

    return ["pytest -q"]


def _infer_source_from_test_paths(paths: list[str]) -> list[str]:
    """tests/test_greeter.py -> greeter.py (when module name not spelled out in ticket)."""
    inferred: list[str] = []
    seen: set[str] = set()
    for path in paths:
        if not path.startswith("tests/test_") or not path.endswith(".py"):
            continue
        stem = path[len("tests/test_") : -3]
        source = f"{stem}.py"
        if source not in seen:
            seen.add(source)
            inferred.append(source)
    return inferred


def build_template_task_plan(ticket: JiraTicket) -> TaskPlan:
    """Deterministic TaskPlan for trivial tickets — no LLM, no Work lane."""
    text = _ticket_text(ticket)
    paths = _paths_in_text(text)
    for inferred in _infer_module_paths(text):
        if inferred not in paths:
            paths.append(inferred)
    for inferred in _infer_source_from_test_paths(paths):
        if inferred not in paths:
            paths.append(inferred)
    paths = _expand_paths_with_tests(paths)
    source_paths = [p for p in paths if not p.startswith("tests/")]
    if not source_paths and paths:
        source_paths = list(paths)

    description = ticket.summary
    if ticket.description.strip():
        description = f"{ticket.summary}. {ticket.description.strip()}"

    # files_to_touch: source files only; test paths inform acceptance_tests, not coverage
    files_to_touch = list(source_paths) if source_paths else list(paths)
    acceptance_tests = _acceptance_tests_from_ticket(ticket, paths)

    return TaskPlan(
        ticket_key=ticket.key,
        summary=ticket.summary,
        steps=[PlanStep(description=description, files=source_paths)],
        files_to_touch=files_to_touch,
        acceptance_tests=acceptance_tests,
        out_of_scope=[],
    )


def build_template_task_plan_validated(ticket: JiraTicket) -> TaskPlan:
    """Build and validate a template TaskPlan; raises on validation failure."""
    plan = build_template_task_plan(ticket)
    try:
        validate_acceptance_tests(plan.acceptance_tests)
        validate_plan_structure(plan, ticket)
    except (AcceptanceTestsValidationError, PlanStructureValidationError) as exc:
        raise RuntimeError(str(exc)) from exc
    return plan


def work_lane_required_for_ticket(ticket: JiraTicket) -> bool:
    """True when TechLead must load the Work vLLM lane (template plan unavailable)."""
    try:
        build_template_task_plan_validated(ticket)
        return False
    except RuntimeError:
        return True
