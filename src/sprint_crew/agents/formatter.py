from __future__ import annotations

from pydantic import ValidationError

from sprint_crew.agents.prompts_formatter import (
    build_formatter_system_prompt,
    build_formatter_user_prompt,
)
from sprint_crew.config import Role
from sprint_crew.inference.structured import structured_completion
from sprint_crew.orchestrator.plan_coverage import DIFF_PATH_RE, normalize_path
from sprint_crew.schemas.change import CodeChange, FileChange
from sprint_crew.schemas.ticket import TaskPlan


def _paths_from_diff(git_diff: str) -> list[str]:
    paths: list[str] = []
    seen: set[str] = set()
    for match in DIFF_PATH_RE.finditer(git_diff):
        path = normalize_path(match.group(1))
        if path and path not in seen:
            seen.add(path)
            paths.append(path)
    return paths


def _derive_files_changed(git_diff: str) -> list[FileChange]:
    return [
        FileChange(path=path, action="modified", summary="changed in workspace diff")
        for path in _paths_from_diff(git_diff)
    ]


def _try_parse_code_change(raw_output: str, task_plan: TaskPlan) -> CodeChange | None:
    text = raw_output.strip()
    if not text.startswith("{"):
        return None
    try:
        change = CodeChange.model_validate_json(text)
    except ValidationError:
        return None
    return change.model_copy(
        update={
            "ticket_key": task_plan.ticket_key,
            "branch": f"feature/{task_plan.ticket_key.lower()}",
        }
    )


def _minimal_code_change_from_handoff(
    *,
    task_plan: TaskPlan,
    raw_output: str,
    git_diff: str,
) -> CodeChange:
    files_changed = _derive_files_changed(git_diff)
    tests_passed = "tests_passed=true" in raw_output.lower() or "tests passed" in raw_output.lower()
    return CodeChange(
        ticket_key=task_plan.ticket_key,
        branch=f"feature/{task_plan.ticket_key.lower()}",
        summary=task_plan.summary,
        files_changed=files_changed,
        tests_passed=tests_passed,
        notes=raw_output[:2000],
    )


async def run_formatter(*, task_plan: TaskPlan, raw_output: str, git_diff: str = "") -> CodeChange:
    parsed = _try_parse_code_change(raw_output, task_plan)
    if parsed is not None:
        if git_diff.strip() and not parsed.files_changed:
            return parsed.model_copy(update={"files_changed": _derive_files_changed(git_diff)})
        return parsed

    plan_json = task_plan.model_dump_json(indent=2)
    try:
        change = structured_completion(
            Role.WORK,
            system_prompt=build_formatter_system_prompt(),
            user_prompt=build_formatter_user_prompt(
                task_plan_json=plan_json,
                raw_output=raw_output,
                git_diff=git_diff,
            ),
            output_type=CodeChange,
        )
    except Exception:
        change = _minimal_code_change_from_handoff(
            task_plan=task_plan,
            raw_output=raw_output,
            git_diff=git_diff,
        )
    else:
        if git_diff.strip():
            derived = _derive_files_changed(git_diff)
            if derived:
                change = change.model_copy(update={"files_changed": derived})

    return change.model_copy(
        update={
            "ticket_key": task_plan.ticket_key,
            "branch": f"feature/{task_plan.ticket_key.lower()}",
        }
    )
