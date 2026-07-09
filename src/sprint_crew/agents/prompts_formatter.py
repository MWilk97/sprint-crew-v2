from __future__ import annotations

GOAL = (
    "Convert the coder's messy final message into a strict CodeChange object "
    "matching the TaskPlan ticket_key and branch naming."
)

BACKSTORY = """You are a single-shot formatter. You receive the TaskPlan and the coder's last message (which may be prose, partial JSON, or invalid JSON).
Extract or reconstruct a valid CodeChange. Do not invent files that were not mentioned. Preserve honest tests_passed reporting.
You have no tools — output CodeChange JSON only."""


def build_formatter_system_prompt() -> str:
    return f"{GOAL}\n\n{BACKSTORY}"


def build_formatter_user_prompt(*, task_plan_json: str, raw_output: str, git_diff: str = "") -> str:
    parts = [
        f"TaskPlan:\n{task_plan_json}\n\n",
        f"Coder raw output:\n{raw_output}\n\n",
    ]
    if git_diff.strip():
        parts.append(f"Git diff (ground truth from workspace):\n{git_diff}\n\n")
    parts.append("Return CodeChange JSON only.")
    return "".join(parts)
