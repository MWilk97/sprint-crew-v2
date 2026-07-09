from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from sprint_crew.agents.reviewer import _run_acceptance_tests
from sprint_crew.agents.tester import run_tester_loop
from sprint_crew.orchestrator.template_plan import build_template_task_plan_validated
from sprint_crew.schemas.change import CodeChange
from sprint_crew.schemas.ticket import JiraTicket
from tests.helpers.vllm_live import docker_available, wait_lane_healthy

_WRITE_TOOLS = frozenset({"write_file", "apply_patch"})

_RETRY_NO_WRITE_FEEDBACK = (
    "\n\nPrevious attempt finished without writing under tests/. "
    "You MUST call write_file or apply_patch on a path under tests/ "
    "(for example tests/test_greeter.py) before handing off."
)


def _workspace_ac_red_for_tester(workspace: Path) -> None:
    """hello() importable but wrong — AC fails on assertion, not ImportError."""
    (workspace / "greeter.py").write_text(
        'def hello():\n    return "wrong"\n',
        encoding="utf-8",
    )


def _tester_wrote_under_tests(tool_log: list[dict], workspace: Path) -> bool:
    return any(entry.get("tool") in _WRITE_TOOLS for entry in tool_log) and _tests_tree_changed(
        str(workspace)
    )


def _tests_tree_changed(workspace: str) -> bool:
    status = subprocess.run(
        ["git", "status", "--porcelain", "tests/"],
        cwd=workspace,
        capture_output=True,
        text=True,
        check=True,
    )
    return bool(status.stdout.strip())


@pytest.mark.agent_live
@pytest.mark.vllm_live
@pytest.mark.asyncio
@pytest.mark.timeout(1200)
async def test_tester_writes_tests_when_ac_red(
    skip_unless_vllm_live,
    tmp_workspace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Real vLLM coder lane: broken greeter fixture, AC red → tester writes under tests/."""
    if not docker_available():
        pytest.skip("docker not available")

    from sprint_crew.config import get_settings

    monkeypatch.setenv("MAX_TESTER_TURNS", "48")
    get_settings.cache_clear()

    settings = get_settings()
    wait_lane_healthy(settings.vllm_coder_url.replace("/v1", "/health"))

    _workspace_ac_red_for_tester(tmp_workspace)

    ticket = JiraTicket(
        key="DEMO-1",
        summary="Add hello() to greeter.py",
        description="Implement hello() returning 'hello' and ensure pytest passes.",
        status="To Do",
        issue_type="Story",
        acceptance_criteria="pytest -q tests/test_greeter.py passes",
    )
    plan = build_template_task_plan_validated(ticket)
    change = CodeChange(
        ticket_key="DEMO-1",
        branch="feature/demo-1",
        summary="partial greeter change without passing tests",
        tests_passed=False,
    )

    acceptance_output, acceptance_green = _run_acceptance_tests(
        tmp_workspace,
        plan.acceptance_tests,
    )
    assert acceptance_green is False

    handoff = ""
    tool_log: list[dict] = []
    extra_output = ""
    for _attempt in range(3):
        handoff, tool_log = await run_tester_loop(
            plan,
            change,
            tmp_workspace,
            acceptance_green=False,
            acceptance_output=(acceptance_output + extra_output).strip(),
        )
        if _tester_wrote_under_tests(tool_log, tmp_workspace):
            break
        extra_output = _RETRY_NO_WRITE_FEEDBACK

    assert handoff.strip()
    assert any(entry.get("tool") in _WRITE_TOOLS for entry in tool_log)
    assert _tests_tree_changed(str(tmp_workspace))


@pytest.mark.agent_live
@pytest.mark.vllm_live
@pytest.mark.asyncio
@pytest.mark.timeout(600)
async def test_tester_reporter_parses_handoff(
    skip_unless_vllm_live,
) -> None:
    """Real work lane JSON — Tester reporter turns handoff text into TestAdditions."""
    if not docker_available():
        pytest.skip("docker not available")

    from sprint_crew.agents.tester import run_tester_reporter
    from sprint_crew.config import get_settings
    from sprint_crew.schemas.ticket import PlanStep, TaskPlan

    settings = get_settings()
    wait_lane_healthy(settings.vllm_work_url.replace("/v1", "/health"))

    plan = TaskPlan(
        ticket_key="DEMO-1",
        summary="Add tests for greeter hello()",
        steps=[PlanStep(description="extend greeter tests", files=["tests/test_greeter.py"])],
        acceptance_tests=["pytest -q tests/test_greeter.py"],
    )
    handoff = (
        "Added tests/test_greeter_extra.py with test_hello_returns_literal. "
        "Ran pytest -q tests/test_greeter.py — all tests passed."
    )

    result = await run_tester_reporter(plan, handoff)

    assert result.ticket_key == "DEMO-1"
    assert result.tests_added
    assert result.coverage_summary.strip()
