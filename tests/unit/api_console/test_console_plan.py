"""Real plan mode and the promote handoff (M10).

The invariant worth the most here is the negative one: plan mode reads the repo and writes
nothing — no Jira issue, no branch, no commit.
"""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from httpx import AsyncClient

from sprint_crew.orchestrator.run_registry import reset_run_registry, run_registry
from sprint_crew.schemas.backlog import BacklogPlan, BacklogStory, ProductBrief
from sprint_crew.schemas.console import ConsoleSessionStatus, WorkspaceStatus
from sprint_crew.schemas.ticket import PlanStep, TaskPlan


@pytest.fixture(autouse=True)
def _clean_registry():
    reset_run_registry()
    yield
    reset_run_registry()


def _backlog(story_count: int = 2) -> BacklogPlan:
    return BacklogPlan(
        product_brief=ProductBrief(
            title="Metrics",
            summary="Expose request metrics",
            goals=["observability"],
        ),
        stories=[
            BacklogStory(
                key=f"DEMO-{i}",
                summary=f"Story {i}",
                description=f"do thing {i}",
                acceptance_criteria="it works",
                priority=i,
                depends_on=[f"DEMO-{i - 1}"] if i > 1 else [],
            )
            for i in range(1, story_count + 1)
        ],
        recommended_first="DEMO-1",
    )


def _task_plan(key: str) -> TaskPlan:
    return TaskPlan(
        ticket_key=key,
        summary=f"plan for {key}",
        steps=[PlanStep(description="edit the router", files=["src/api.py"])],
        files_to_touch=["src/api.py"],
        acceptance_tests=["pytest tests/test_api.py"],
    )


@contextmanager
def _agents(plan: BacklogPlan | None = None, *, tech_lead=None, error: Exception | None = None):
    """Stub the two model calls and the lane. Neither belongs in a unit test."""
    scrum = AsyncMock(side_effect=error) if error else AsyncMock(return_value=plan or _backlog())
    lead = tech_lead or AsyncMock(return_value=(_task_plan("DEMO-1"), "tool_loop"))
    with (
        patch("sprint_crew.orchestrator.plan_run.run_scrum_master", scrum),
        patch("sprint_crew.orchestrator.plan_run.run_tech_lead", lead),
        patch("sprint_crew.orchestrator.plan_run.ensure_lane", AsyncMock()),
        patch("sprint_crew.orchestrator.plan_run.stop_lane", AsyncMock()),
        patch(
            "sprint_crew.orchestrator.plan_run.enrich_repo_context",
            lambda *a, **k: "=== repo ===",
        ),
    ):
        yield scrum, lead


async def _ready_plan_session(api_client: AsyncClient, tmp_path: Path, **overrides) -> str:
    """A confirmed plan session whose checkout is ready, without cloning anything."""
    with patch("sprint_crew.api.console.routes.schedule_workspace_prep"):
        created = await api_client.post(
            "/v1/console/sessions",
            json={"mode": "plan", "initial_prompt": "Add a /metrics endpoint"},
        )
    session = created.json()
    session_id = session["session_id"]

    answers = [
        {
            "question_id": q["question_id"],
            "selected_suggestion_id": q["suggestions"][0]["suggestion_id"],
            "custom_text": None,
        }
        for q in session["clarify_questions"]
    ]
    await api_client.post(f"/v1/console/sessions/{session_id}/clarify", json={"answers": answers})
    await api_client.post(f"/v1/console/sessions/{session_id}/confirm")

    workspace = tmp_path / "ws" / session_id
    workspace.mkdir(parents=True, exist_ok=True)

    from sprint_crew.orchestrator.console_store import console_store

    store = console_store()
    stored = store.load(session_id)
    assert stored is not None
    stored.workspace_status = WorkspaceStatus.READY
    stored.workspace_root = str(workspace)
    for key, value in overrides.items():
        setattr(stored, key, value)
    store.save(stored)
    return session_id


async def _drain(api_client: AsyncClient, session_id: str) -> dict:
    """Let the queued plan body finish, then return the session."""
    for _ in range(400):
        session = (await api_client.get(f"/v1/console/sessions/{session_id}")).json()
        if session["status"] not in ("queued", "running"):
            return session
        await asyncio.sleep(0.01)
    raise AssertionError("plan run did not finish")


async def _events(api_client: AsyncClient, session_id: str) -> list[dict]:
    response = await api_client.get(f"/v1/console/sessions/{session_id}/events")
    return response.json()["events"]


# --- the run ------------------------------------------------------------------


async def test_plan_start_returns_202_before_the_model_runs(
    api_client: AsyncClient, tmp_path: Path
) -> None:
    session_id = await _ready_plan_session(api_client, tmp_path)
    with _agents():
        response = await api_client.post(f"/v1/console/sessions/{session_id}/start")

        assert response.status_code == 202
        body = response.json()
        assert body["status"] in ("queued", "running")
        assert body["plan_result"] is None
        assert body["plan_run_id"].startswith("plan-")
        await _drain(api_client, session_id)


async def test_quick_plan_uses_scrum_master_titles_and_skips_the_tech_lead(
    api_client: AsyncClient, tmp_path: Path
) -> None:
    """The whole point of M10: story titles come from the model, not from the prompt."""
    session_id = await _ready_plan_session(api_client, tmp_path)
    with _agents() as (scrum, lead):
        await api_client.post(f"/v1/console/sessions/{session_id}/start", json={"depth": "quick"})
        session = await _drain(api_client, session_id)

    assert session["status"] == "completed"
    result = session["plan_result"]
    assert result["depth"] == "quick"
    assert [s["title"] for s in result["stories"]] == ["Story 1", "Story 2"]
    assert result["summary"] == "Expose request metrics"
    assert result["product_brief"]["title"] == "Metrics"
    assert result["recommended_first"] == "DEMO-1"
    # ScrumMaster fields survive; TechLead ones stay empty and depth says why.
    assert result["stories"][1]["depends_on"] == ["DEMO-1"]
    assert result["stories"][0]["files_to_touch"] == []
    assert scrum.await_count == 1
    assert lead.await_count == 0


async def test_deep_plan_adds_tech_lead_detail_per_story(
    api_client: AsyncClient, tmp_path: Path
) -> None:
    session_id = await _ready_plan_session(api_client, tmp_path)
    with _agents() as (_scrum, lead):
        await api_client.post(f"/v1/console/sessions/{session_id}/start", json={"depth": "deep"})
        session = await _drain(api_client, session_id)

    result = session["plan_result"]
    assert result["depth"] == "deep"
    assert lead.await_count == 2  # one per story
    story = result["stories"][0]
    assert story["files_to_touch"] == ["src/api.py"]
    assert story["acceptance_tests"] == ["pytest tests/test_api.py"]
    assert story["steps"] == ["edit the router"]
    # A template plan is not repo analysis; the UI needs to be able to tell.
    assert story["planning_mode"] == "tool_loop"


async def test_one_story_failing_to_plan_does_not_lose_the_backlog(
    api_client: AsyncClient, tmp_path: Path
) -> None:
    session_id = await _ready_plan_session(api_client, tmp_path)
    lead = AsyncMock(side_effect=[RuntimeError("lane died"), (_task_plan("DEMO-2"), "static")])
    with _agents(tech_lead=lead):
        await api_client.post(f"/v1/console/sessions/{session_id}/start", json={"depth": "deep"})
        session = await _drain(api_client, session_id)

    assert session["status"] == "completed"
    stories = session["plan_result"]["stories"]
    assert stories[0]["files_to_touch"] == []
    assert stories[1]["files_to_touch"] == ["src/api.py"]


async def test_plan_emits_a_timeline_the_ui_can_render(
    api_client: AsyncClient, tmp_path: Path
) -> None:
    session_id = await _ready_plan_session(api_client, tmp_path)
    with _agents():
        await api_client.post(f"/v1/console/sessions/{session_id}/start", json={"depth": "quick"})
        await _drain(api_client, session_id)
    events = await _events(api_client, session_id)

    kinds = [e["event_type"] for e in events]
    assert (
        kinds.index("plan_started")
        < kinds.index("backlog_planned")
        < kinds.index("story_planned")
        < kinds.index("plan_complete")
    )
    complete = next(e for e in events if e["event_type"] == "plan_complete")
    assert complete["detail"]["story_count"] == 2


async def test_a_failing_scrum_master_fails_the_session_with_a_reason(
    api_client: AsyncClient, tmp_path: Path
) -> None:
    session_id = await _ready_plan_session(api_client, tmp_path)
    with _agents(error=RuntimeError("model exploded")):
        await api_client.post(f"/v1/console/sessions/{session_id}/start")
        session = await _drain(api_client, session_id)

    assert session["status"] == "failed"
    assert "model exploded" in session["error"]
    failed = next(
        e for e in await _events(api_client, session_id) if e["event_type"] == "plan_failed"
    )
    assert "model exploded" in failed["detail"]["error"]


async def test_plan_mode_creates_no_jira_issue(api_client: AsyncClient, tmp_path: Path) -> None:
    """``create_jira_tickets`` really does POST to Jira; plan mode must never reach it."""
    session_id = await _ready_plan_session(api_client, tmp_path)
    with (
        _agents(),
        patch("sprint_crew.orchestrator.backlog.create_jira_tickets") as jira,
    ):
        await api_client.post(f"/v1/console/sessions/{session_id}/start", json={"depth": "deep"})
        await _drain(api_client, session_id)

    jira.assert_not_called()


async def test_plan_without_a_checkout_fails_rather_than_hanging(
    api_client: AsyncClient, tmp_path: Path, monkeypatch
) -> None:
    from sprint_crew.config import get_settings

    session_id = await _ready_plan_session(
        api_client, tmp_path, workspace_status=WorkspaceStatus.FAILED
    )
    monkeypatch.setenv("CONSOLE_PLAN_WORKSPACE_WAIT_S", "0.05")
    get_settings.cache_clear()

    with _agents():
        await api_client.post(f"/v1/console/sessions/{session_id}/start")
        session = await _drain(api_client, session_id)

    assert session["status"] == "failed"
    assert "repository is failed" in session["error"]


# --- queue and cancel ---------------------------------------------------------


async def test_plan_run_waits_behind_another_run(api_client: AsyncClient, tmp_path: Path) -> None:
    """A plan run takes the same single slot a code run does — it needs the same lane."""
    session_id = await _ready_plan_session(api_client, tmp_path)
    started = asyncio.Event()

    async def occupy() -> None:
        started.set()
        await asyncio.sleep(60)

    run_registry().submit("run-other", occupy)
    await started.wait()

    with _agents():
        response = await api_client.post(f"/v1/console/sessions/{session_id}/start")

    assert response.json()["status"] == "queued"
    assert response.json()["queue_position"] == 1


async def test_cancelling_a_queued_plan_never_runs_the_model(
    api_client: AsyncClient, tmp_path: Path
) -> None:
    session_id = await _ready_plan_session(api_client, tmp_path)
    started = asyncio.Event()

    async def occupy() -> None:
        started.set()
        await asyncio.sleep(60)

    run_registry().submit("run-other", occupy)
    await started.wait()

    with _agents() as (scrum, _lead):
        await api_client.post(f"/v1/console/sessions/{session_id}/start")
        cancelled = await api_client.post(f"/v1/console/sessions/{session_id}/cancel")

    assert cancelled.status_code == 200
    assert cancelled.json()["status"] == "cancelled"
    assert cancelled.json()["queue_position"] is None
    await asyncio.sleep(0.05)
    assert scrum.await_count == 0


async def test_cancelling_a_running_plan_stops_between_stories(
    api_client: AsyncClient, tmp_path: Path
) -> None:
    """Cancel is cooperative: the checkpoint is between stories, as in the batch loop."""
    session_id = await _ready_plan_session(api_client, tmp_path)
    first_story = asyncio.Event()

    async def slow_lead(*_args, **_kwargs):
        first_story.set()
        await asyncio.sleep(0.05)
        return _task_plan("DEMO-1"), "static"

    with _agents(tech_lead=slow_lead):
        await api_client.post(f"/v1/console/sessions/{session_id}/start", json={"depth": "deep"})
        await asyncio.wait_for(first_story.wait(), timeout=5)
        await api_client.post(f"/v1/console/sessions/{session_id}/cancel")
        session = await _drain(api_client, session_id)

    assert session["status"] == "cancelled"
    assert session["plan_result"] is None


async def test_messages_are_refused_while_a_plan_run_is_live(
    api_client: AsyncClient, tmp_path: Path
) -> None:
    session_id = await _ready_plan_session(api_client, tmp_path)
    release = asyncio.Event()

    async def slow_scrum(**_kwargs):
        await release.wait()
        return _backlog()

    with (
        patch("sprint_crew.orchestrator.plan_run.run_scrum_master", slow_scrum),
        patch("sprint_crew.orchestrator.plan_run.ensure_lane", AsyncMock()),
        patch("sprint_crew.orchestrator.plan_run.stop_lane", AsyncMock()),
        patch(
            "sprint_crew.orchestrator.plan_run.enrich_repo_context", lambda *a, **k: "=== repo ==="
        ),
    ):
        await api_client.post(f"/v1/console/sessions/{session_id}/start")
        await asyncio.sleep(0.05)

        response = await api_client.post(
            f"/v1/console/sessions/{session_id}/messages", json={"content": "actually, wait"}
        )
        assert response.status_code == 409

        release.set()
        await _drain(api_client, session_id)


# --- promote ------------------------------------------------------------------


async def _completed_plan(api_client: AsyncClient, tmp_path: Path) -> str:
    session_id = await _ready_plan_session(api_client, tmp_path)
    with _agents():
        await api_client.post(f"/v1/console/sessions/{session_id}/start")
        await _drain(api_client, session_id)
    return session_id


async def test_promote_creates_a_child_code_session_running_the_reviewed_backlog(
    api_client: AsyncClient, tmp_path: Path
) -> None:
    parent_id = await _completed_plan(api_client, tmp_path)
    start = AsyncMock(return_value="run-91c2")

    with (
        patch("sprint_crew.api.app.start_from_prompt_run", start),
        patch("sprint_crew.api.console.plan.schedule_workspace_prep"),
    ):
        response = await api_client.post(f"/v1/console/sessions/{parent_id}/promote")

    assert response.status_code == 202
    child = response.json()
    assert child["session_id"] != parent_id
    assert child["mode"] == "code"
    assert child["parent_session_id"] == parent_id
    assert child["confirmed"] is True
    assert child["status"] in ("queued", "running")
    assert child["sprint_ref"]["backlog_run_id"] == "run-91c2"
    # The reviewed backlog is handed over, not re-derived.
    passed_plan = start.await_args.kwargs["plan"]
    assert [s.key for s in passed_plan.stories] == ["DEMO-1", "DEMO-2"]

    # The plan session stays terminal and readable — history shows two rows, not one.
    parent = (await api_client.get(f"/v1/console/sessions/{parent_id}")).json()
    assert parent["status"] == "completed"
    assert parent["plan_result"]["stories"]


async def test_promoted_run_does_not_re_plan(api_client: AsyncClient, tmp_path: Path) -> None:
    """Fidelity, not speed: a second ScrumMaster could return a different backlog."""
    parent_id = await _completed_plan(api_client, tmp_path)
    stored = (await api_client.get(f"/v1/console/sessions/{parent_id}")).json()["plan_result"]

    from sprint_crew.orchestrator.prompt_run import run_from_prompt

    with (
        patch("sprint_crew.orchestrator.prompt_run._plan") as replan,
        patch("sprint_crew.orchestrator.prompt_run.run_backlog_batched", AsyncMock()) as batched,
    ):
        await run_from_prompt(
            run_id="run-1",
            prompt="anything",
            repo_url=None,
            use_real_ship=False,
            plan=_backlog(),
        )

    replan.assert_not_called()
    assert [s.key for s in batched.await_args.kwargs["plan"].stories] == ["DEMO-1", "DEMO-2"]
    assert len(stored["stories"]) == 2


async def test_promote_carries_the_conversation_without_re_clarifying(
    api_client: AsyncClient, tmp_path: Path
) -> None:
    parent_id = await _completed_plan(api_client, tmp_path)

    with (
        patch("sprint_crew.api.app.start_from_prompt_run", AsyncMock(return_value="run-1")),
        patch("sprint_crew.api.console.plan.schedule_workspace_prep"),
    ):
        child = (await api_client.post(f"/v1/console/sessions/{parent_id}/promote")).json()

    assert child["messages"][0]["content"] == "Add a /metrics endpoint"
    assert child["clarify_questions"] == []


async def test_promote_records_the_handoff_on_the_plan_timeline(
    api_client: AsyncClient, tmp_path: Path
) -> None:
    parent_id = await _completed_plan(api_client, tmp_path)

    with (
        patch("sprint_crew.api.app.start_from_prompt_run", AsyncMock(return_value="run-1")),
        patch("sprint_crew.api.console.plan.schedule_workspace_prep"),
    ):
        child = (await api_client.post(f"/v1/console/sessions/{parent_id}/promote")).json()

    promoted = next(
        e for e in await _events(api_client, parent_id) if e["event_type"] == "promoted"
    )
    assert promoted["detail"]["child_session_id"] == child["session_id"]


async def test_promote_needs_a_completed_plan(api_client: AsyncClient, tmp_path: Path) -> None:
    session_id = await _ready_plan_session(api_client, tmp_path)

    response = await api_client.post(f"/v1/console/sessions/{session_id}/promote")

    assert response.status_code == 409
    assert "needs a completed plan" in response.json()["detail"]


async def test_promote_refuses_a_code_session(api_client: AsyncClient, tmp_path: Path) -> None:
    with patch("sprint_crew.api.console.routes.schedule_workspace_prep"):
        created = await api_client.post("/v1/console/sessions", json={"mode": "code"})
    session_id = created.json()["session_id"]

    response = await api_client.post(f"/v1/console/sessions/{session_id}/promote")

    assert response.status_code == 409
    assert "promote needs plan" in response.json()["detail"]


async def test_promote_on_unknown_session_is_404(api_client: AsyncClient) -> None:
    assert (await api_client.post("/v1/console/sessions/cs-nope/promote")).status_code == 404


async def test_deleting_a_plan_session_drops_its_stored_backlog(
    api_client: AsyncClient, tmp_path: Path
) -> None:
    """Otherwise a promote-after-delete would run a backlog nobody can read any more."""
    from sprint_crew.orchestrator.plan_store import plan_store

    parent_id = await _completed_plan(api_client, tmp_path)
    assert plan_store().load(parent_id) is not None

    assert (await api_client.delete(f"/v1/console/sessions/{parent_id}")).status_code == 204

    assert plan_store().load(parent_id) is None


async def test_a_plan_session_reports_its_status_like_any_run(
    api_client: AsyncClient, tmp_path: Path
) -> None:
    """Status is derived, so a completed plan must survive a re-read unchanged (M10)."""
    session_id = await _completed_plan(api_client, tmp_path)

    first = (await api_client.get(f"/v1/console/sessions/{session_id}")).json()
    second = (await api_client.get(f"/v1/console/sessions/{session_id}")).json()

    assert first["status"] == second["status"] == ConsoleSessionStatus.COMPLETED.value
    assert second["plan_result"]["stories"]
