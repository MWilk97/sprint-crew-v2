from __future__ import annotations

import json
import re
import sqlite3
from collections import deque
from pathlib import Path

from sprint_crew.config import get_settings
from sprint_crew.integrations.jira_client import get_jira_client
from sprint_crew.orchestrator.complexity import PromptComplexity, assess_prompt_complexity
from sprint_crew.schemas.backlog import BacklogPlan, BacklogStory
from sprint_crew.schemas.session import BacklogRun
from sprint_crew.schemas.ticket import JiraTicket


def _merge_stories_to_one(plan: BacklogPlan) -> BacklogPlan:
    if len(plan.stories) <= 1:
        return plan
    first = plan.stories[0]
    descriptions = [story.description for story in plan.stories if story.description.strip()]
    criteria = [
        story.acceptance_criteria for story in plan.stories if story.acceptance_criteria.strip()
    ]
    merged = BacklogStory(
        key=first.key,
        summary=plan.product_brief.title or first.summary,
        description="\n\n".join(descriptions),
        issue_type=first.issue_type,
        acceptance_criteria="\n".join(criteria) or first.acceptance_criteria,
        priority=first.priority,
        depends_on=[],
    )
    return plan.model_copy(update={"stories": [merged], "recommended_first": merged.key})


_SPLIT_STORY_CAP_RE = re.compile(
    r"split\s+into\s+(?:2[\s–-]3|2\s+to\s+3|two[\s–-]three)(?:\s+\w+)*\s+stor",
    re.IGNORECASE,
)
_GREETER_ONLY_RE = re.compile(
    r"greeter\s+tests?\s+(?:unaffected|unchanged|still\s+pass)",
    re.IGNORECASE,
)


def _prompt_story_cap(user_prompt: str, default_cap: int) -> int:
    if _SPLIT_STORY_CAP_RE.search(user_prompt):
        return 3
    return default_cap


def _drop_greeter_only_stories(plan: BacklogPlan) -> BacklogPlan:
    kept = [
        story
        for story in plan.stories
        if not (
            _GREETER_ONLY_RE.search(story.summary)
            or _GREETER_ONLY_RE.search(story.description)
            or _GREETER_ONLY_RE.search(story.acceptance_criteria)
        )
    ]
    if not kept or len(kept) == len(plan.stories):
        return plan
    kept_keys = {story.key for story in kept}
    recommended = plan.recommended_first
    if recommended not in kept_keys:
        recommended = kept[0].key
    return plan.model_copy(update={"stories": kept, "recommended_first": recommended})


def normalize_backlog_plan(plan: BacklogPlan, *, user_prompt: str) -> BacklogPlan:
    """Cap story count and collapse over-split trivial requests."""
    settings = get_settings()
    complexity = assess_prompt_complexity(user_prompt)
    normalized = plan

    if complexity == PromptComplexity.TRIVIAL and len(normalized.stories) > 1:
        normalized = _merge_stories_to_one(normalized)

    normalized = _drop_greeter_only_stories(normalized)

    max_stories = _prompt_story_cap(user_prompt, settings.max_backlog_stories)
    ordered = sort_stories(normalized)
    if len(ordered) > max_stories:
        kept = ordered[:max_stories]
        kept_keys = {story.key for story in kept}
        recommended = normalized.recommended_first
        if recommended not in kept_keys:
            recommended = kept[0].key
        normalized = normalized.model_copy(
            update={
                "stories": kept,
                "recommended_first": recommended,
            }
        )
    return normalized


def sort_stories(plan: BacklogPlan) -> list[BacklogStory]:
    """Return stories in dependency order (depends_on respected)."""
    by_key = {story.key: story for story in plan.stories}
    indegree: dict[str, int] = {story.key: 0 for story in plan.stories}
    graph: dict[str, list[str]] = {story.key: [] for story in plan.stories}

    for story in plan.stories:
        for dep in story.depends_on:
            if dep not in by_key:
                continue
            graph[dep].append(story.key)
            indegree[story.key] += 1

    queue = deque(sorted(key for key, degree in indegree.items() if degree == 0))
    ordered: list[BacklogStory] = []
    while queue:
        key = queue.popleft()
        ordered.append(by_key[key])
        for child in graph[key]:
            indegree[child] -= 1
            if indegree[child] == 0:
                queue.append(child)

    if len(ordered) != len(plan.stories):
        remaining = [story for story in plan.stories if story not in ordered]
        ordered.extend(remaining)
    return ordered


class BacklogRunStore:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS backlog_runs (
                    run_id TEXT PRIMARY KEY,
                    payload TEXT NOT NULL
                )
                """
            )

    def save(self, run: BacklogRun) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO backlog_runs (run_id, payload)
                VALUES (?, ?)
                ON CONFLICT(run_id) DO UPDATE SET payload=excluded.payload
                """,
                (run.run_id, json.dumps(run.model_dump(mode="json"))),
            )

    def load(self, run_id: str) -> BacklogRun | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT payload FROM backlog_runs WHERE run_id = ?",
                (run_id,),
            ).fetchone()
        if row is None:
            return None
        return BacklogRun.model_validate(json.loads(row["payload"]))


def _backlog_store() -> BacklogRunStore:
    return BacklogRunStore(get_settings().session_db)


def create_jira_tickets(plan: BacklogPlan) -> dict[str, JiraTicket]:
    settings = get_settings()
    jira = get_jira_client()
    tickets: dict[str, JiraTicket] = {}
    for story in sort_stories(plan):
        ticket = jira.create_issue(
            project_key=settings.jira_project_key,
            summary=story.summary,
            description=story.description,
            issue_type=story.issue_type.value,
            acceptance_criteria=story.acceptance_criteria,
        )
        tickets[story.key] = ticket
    return tickets


async def run_backlog(
    *,
    run_id: str,
    plan: BacklogPlan,
    user_prompt: str,
    repo_url: str | None = None,
    use_real_ship: bool = False,
) -> BacklogRun:
    from sprint_crew.orchestrator.batch_cycle import run_backlog_batched

    return await run_backlog_batched(
        run_id=run_id,
        plan=plan,
        user_prompt=user_prompt,
        repo_url=repo_url,
        use_real_ship=use_real_ship,
    )


def get_backlog_run(run_id: str) -> BacklogRun | None:
    return _backlog_store().load(run_id)
