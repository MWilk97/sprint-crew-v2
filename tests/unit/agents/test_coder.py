from __future__ import annotations

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pydantic_ai.exceptions import ModelAPIError, UsageLimitExceeded

from sprint_crew.agents.coder import (
    _tool_log_has_repeated_call,
    _tool_log_is_stuck_on_refusals,
)
from sprint_crew.config import Role, get_settings
from sprint_crew.inference.router import (
    coder_model_settings,
    coder_thinking_active,
    pydantic_ai_model,
)
from sprint_crew.orchestrator.plan_coverage import PlanCoverageResult
from sprint_crew.schemas.ticket import PlanStep, TaskPlan
from sprint_crew.tools.pydantic_ai import (
    _coder_scope_error_for_path,
    _record_tool_call,
    workspace_deps,
)


def _queue_plan() -> TaskPlan:
    return TaskPlan(
        ticket_key="DEMO-1",
        summary="queue worker",
        steps=[PlanStep(description="wire queue", files=["src/messaging/queue_worker.py"])],
        files_to_touch=["src/messaging/queue_worker.py"],
        acceptance_tests=["pytest -q tests/test_ferry_queue.py"],
        out_of_scope=["src/messaging/retry_policy.py"],
    )


def test_coder_write_file_rejects_out_of_scope(tmp_path) -> None:
    deps = workspace_deps(tmp_path, mutate=True, task_plan=_queue_plan())
    err = _coder_scope_error_for_path(deps, "src/messaging/retry_policy.py")
    assert err is not None
    assert "out_of_scope" in err


def test_coder_write_file_allows_planned_path(tmp_path) -> None:
    deps = workspace_deps(tmp_path, mutate=True, task_plan=_queue_plan())
    assert _coder_scope_error_for_path(deps, "src/messaging/queue_worker.py") is None


def test_coder_read_file_unrestricted_with_task_plan(tmp_path) -> None:
    deps = workspace_deps(tmp_path, mutate=True, task_plan=_queue_plan())
    ferry = tmp_path / "src/messaging"
    ferry.mkdir(parents=True)
    (ferry / "ferry.py").write_text("def send():\n    pass\n", encoding="utf-8")
    result = deps.registry.dispatch(
        "read_file",
        {"path": "src/messaging/ferry.py"},
        workspace_root=tmp_path,
    )
    assert result.ok
    assert "send" in result.output


def test_workspace_deps_write_file_via_registry(tmp_path) -> None:
    deps = workspace_deps(tmp_path, mutate=True)
    result = deps.registry.dispatch(
        "write_file",
        {"path": "greeter.py", "content": 'def hello():\n    return "hello"\n'},
        workspace_root=tmp_path,
    )
    assert result.ok
    assert (tmp_path / "greeter.py").read_text(encoding="utf-8").startswith("def hello")


def test_record_tool_call_appends_to_log(tmp_path) -> None:
    tool_log: list[dict] = []
    deps = workspace_deps(tmp_path, mutate=True, tool_call_log=tool_log)
    _record_tool_call(
        deps,
        "write_file",
        {"path": "greeter.py", "content": "x"},
        "written",
        ok=True,
    )
    assert len(tool_log) == 1
    assert tool_log[0]["tool"] == "write_file"


def test_tool_log_has_repeated_call_detects_same_invocation() -> None:
    log = [
        {"tool": "read_file", "args": {"path": "a.py"}},
        {"tool": "read_file", "args": {"path": "a.py"}},
        {"tool": "read_file", "args": {"path": "a.py"}},
    ]
    assert _tool_log_has_repeated_call(log, threshold=3) is True


def test_tool_log_has_repeated_call_ignores_mixed_tools() -> None:
    log = [
        {"tool": "read_file", "args": {"path": "a.py"}},
        {"tool": "write_file", "args": {"path": "b.py"}},
        {"tool": "grep", "args": {"pattern": "hello"}},
    ]
    assert _tool_log_has_repeated_call(log, threshold=3) is False


class _RaisingRun:
    result = None

    def __aiter__(self):
        return self

    async def __anext__(self):
        raise ModelAPIError(model_name="laguna-s-2.1-nvfp4", message="Request timed out.")


class _RaisingIterCM:
    async def __aenter__(self):
        return _RaisingRun()

    async def __aexit__(self, *exc):
        return False


class _RaisingAgent:
    def iter(self, *args, **kwargs):
        return _RaisingIterCM()


class _StubDeps:
    early_exit_handoff = ""


@pytest.mark.asyncio
async def test_run_coder_loop_hands_off_on_model_api_error(tmp_path, monkeypatch) -> None:
    from sprint_crew.agents import coder

    monkeypatch.setattr(
        coder,
        "_build_coder_agent",
        lambda *a, **k: (_RaisingAgent(), _StubDeps()),
    )

    raw_output, tool_log = await coder.run_coder_loop(_queue_plan(), tmp_path)

    assert isinstance(raw_output, str)
    assert "handing off partial work" in raw_output
    assert tool_log == []


@pytest.fixture
def plan_with_missing_source() -> TaskPlan:
    return TaskPlan(
        ticket_key="DEMO-1",
        summary="add feature",
        steps=[PlanStep(description="create module", files=["feature.py"])],
        files_to_touch=["feature.py"],
        acceptance_tests=["pytest -q"],
    )


@pytest.mark.asyncio
async def test_run_coder_with_coverage_skips_unfixable_continuation(
    plan_with_missing_source: TaskPlan,
    tmp_path,
) -> None:
    from sprint_crew.agents import coder, coder_coverage
    from sprint_crew.orchestrator import plan_coverage

    unsatisfied = PlanCoverageResult(
        missing=["tests/test_feature.py"],
        unexpected=[],
        out_of_scope_hits=[],
        satisfied=False,
    )
    with (
        patch.object(coder, "run_coder_plan", new=AsyncMock(return_value=("done", []))),
        patch.object(plan_coverage, "validate_plan_coverage", return_value=unsatisfied),
        patch.object(plan_coverage, "continuation_makes_sense", return_value=False),
        patch.object(coder, "run_coder_loop", new=AsyncMock()) as loop_mock,
    ):
        _, _, coverage, _, _ = await coder_coverage.run_coder_with_coverage(
            plan_with_missing_source, tmp_path
        )

    loop_mock.assert_not_awaited()
    assert coverage.satisfied is False


@pytest.mark.asyncio
async def test_run_coder_with_coverage_stops_on_stall(
    plan_with_missing_source: TaskPlan, tmp_path
) -> None:
    from sprint_crew.agents import coder, coder_coverage
    from sprint_crew.orchestrator import plan_coverage

    stalled = PlanCoverageResult(
        missing=["feature.py"],
        unexpected=[],
        out_of_scope_hits=[],
        satisfied=False,
    )

    with (
        patch.object(coder, "run_coder_plan", new=AsyncMock(return_value=("done", []))),
        patch.object(plan_coverage, "validate_plan_coverage", side_effect=[stalled, stalled]),
        patch.object(plan_coverage, "continuation_makes_sense", return_value=True),
        patch.object(
            coder, "run_coder_loop", new=AsyncMock(return_value=("still missing", []))
        ) as loop_mock,
    ):
        _, _, coverage, _, _ = await coder_coverage.run_coder_with_coverage(
            plan_with_missing_source, tmp_path
        )

    loop_mock.assert_awaited_once()
    assert coverage.satisfied is False


@pytest.mark.asyncio
async def test_run_coder_loop_usage_limit_returns_partial(tmp_path) -> None:
    from sprint_crew.agents import coder

    plan = TaskPlan(
        ticket_key="DEMO-1",
        summary="single",
        steps=[PlanStep(description="one", files=["a.py"])],
        acceptance_tests=["pytest -q"],
    )

    class FakeIter:
        async def __aenter__(self):
            raise UsageLimitExceeded("limit")

        async def __aexit__(self, *args):
            return False

    class FakeAgent:
        def iter(self, *args, **kwargs):
            return FakeIter()

    _agent, deps = coder._build_coder_agent(tmp_path, plan)
    with patch.object(coder, "_build_coder_agent", return_value=(FakeAgent(), deps)):
        output, log = await coder.run_coder_loop(plan, tmp_path, max_turns=3)

    assert "exhausted" in output.lower()
    assert log == []


@pytest.mark.asyncio
async def test_run_coder_loop_handles_none_result(tmp_path) -> None:
    from sprint_crew.agents import coder

    plan = TaskPlan(
        ticket_key="DEMO-1",
        summary="edit",
        steps=[PlanStep(description="edit", files=["a.py"])],
        acceptance_tests=["pytest -q"],
    )

    @asynccontextmanager
    async def fake_iter(*_args, **_kwargs):
        run = MagicMock()
        run.result = None

        async def empty_iter():
            if False:
                yield None

        run.__aiter__ = lambda self: empty_iter()
        yield run

    agent_mock = MagicMock()
    agent_mock.iter = fake_iter

    with patch.object(
        coder, "_build_coder_agent", return_value=(agent_mock, MagicMock(early_exit_handoff=None))
    ):
        handoff, tool_log = await coder.run_coder_loop(plan, tmp_path)

    assert handoff == "Coder loop ended without structured handoff."
    assert tool_log == []


@pytest.mark.asyncio
async def test_run_coder_loop_handles_model_http_error(tmp_path) -> None:
    """A mid-loop HTTP 400 (context overflow) hands off partial work, not a crash."""
    from pydantic_ai.exceptions import ModelHTTPError

    from sprint_crew.agents import coder

    plan = TaskPlan(
        ticket_key="DEMO-1",
        summary="edit",
        steps=[PlanStep(description="edit", files=["a.py"])],
        acceptance_tests=["pytest -q"],
    )

    @asynccontextmanager
    async def fake_iter(*_args, **_kwargs):
        run = MagicMock()
        run.result = None

        async def raising_iter():
            raise ModelHTTPError(
                status_code=400, model_name="qwen3-coder-next", body={"message": "too long"}
            )
            yield None  # pragma: no cover

        run.__aiter__ = lambda self: raising_iter()
        yield run

    agent_mock = MagicMock()
    agent_mock.iter = fake_iter

    with patch.object(
        coder, "_build_coder_agent", return_value=(agent_mock, MagicMock(early_exit_handoff=None))
    ):
        handoff, _tool_log = await coder.run_coder_loop(plan, tmp_path)

    assert "handing off partial work" in handoff
    assert "400" in handoff


@pytest.fixture
def multi_step_plan() -> TaskPlan:
    return TaskPlan(
        ticket_key="DEMO-2",
        summary="multi file",
        steps=[
            PlanStep(description="create models", files=["models.py"]),
            PlanStep(description="create api", files=["api.py"]),
        ],
        files_to_touch=["models.py", "api.py"],
        acceptance_tests=["pytest -q"],
    )


@pytest.mark.asyncio
async def test_coder_plan_invokes_separate_loop_per_step_when_step_mode_on(
    multi_step_plan: TaskPlan,
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from sprint_crew.agents import coder

    monkeypatch.setenv("CODER_STEP_MODE", "true")
    from sprint_crew.config import get_settings

    get_settings.cache_clear()

    with patch.object(
        coder, "run_coder_loop", new=AsyncMock(return_value=("step done", []))
    ) as loop_mock:
        await coder.run_coder_plan(multi_step_plan, tmp_path)

    assert loop_mock.await_count == len(multi_step_plan.steps)
    get_settings.cache_clear()


@pytest.mark.asyncio
async def test_turn_budget_per_step_applies_lane_multiplier(
    multi_step_plan: TaskPlan,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from sprint_crew.agents import coder
    from sprint_crew.config import get_settings

    monkeypatch.setenv("MAX_CODER_TURNS", "32")
    get_settings.cache_clear()

    # 2 steps: effective total int(32*1.25)=40 → 20 per step (not raw 16).
    assert coder._turn_budget_per_step(multi_step_plan) == 20
    get_settings.cache_clear()


@pytest.mark.asyncio
async def test_coder_plan_single_loop_when_step_mode_off(
    multi_step_plan: TaskPlan,
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from sprint_crew.agents import coder

    monkeypatch.setenv("CODER_STEP_MODE", "false")
    from sprint_crew.config import get_settings

    get_settings.cache_clear()

    with patch.object(
        coder, "run_coder_loop", new=AsyncMock(return_value=("done", []))
    ) as loop_mock:
        await coder.run_coder_plan(multi_step_plan, tmp_path)

    loop_mock.assert_awaited_once()
    get_settings.cache_clear()


@pytest.mark.asyncio
async def test_coder_plan_single_loop_for_single_step_plan(tmp_path) -> None:
    """A plan with only one step takes the fast path regardless of step-mode setting."""
    from sprint_crew.agents import coder

    single_step_plan = TaskPlan(
        ticket_key="DEMO-1",
        summary="single",
        steps=[PlanStep(description="one", files=["a.py"])],
        acceptance_tests=["pytest -q"],
    )
    with patch.object(
        coder, "run_coder_loop", new=AsyncMock(return_value=("done", []))
    ) as loop_mock:
        await coder.run_coder_plan(single_step_plan, tmp_path)

    loop_mock.assert_awaited_once()


def _reset_settings() -> None:
    get_settings.cache_clear()


def test_coder_sampling_pinned_to_spec() -> None:
    _reset_settings()
    settings = coder_model_settings(attempt=0)
    assert settings.get("temperature") == 0.7
    assert settings.get("top_p") == 0.95
    assert settings.get("extra_body", {}).get("top_k") == 20


def test_thinking_off_before_escalation_attempt() -> None:
    _reset_settings()
    threshold = get_settings().coder_thinking_escalation_attempt
    for attempt in range(threshold):
        assert coder_thinking_active(attempt) is False
        settings = coder_model_settings(attempt=attempt)
        assert "chat_template_kwargs" not in settings.get("extra_body", {})
        assert settings.get("timeout") == get_settings().coder_request_timeout_s


def test_thinking_on_and_longer_timeout_from_escalation_attempt() -> None:
    _reset_settings()
    threshold = get_settings().coder_thinking_escalation_attempt
    assert coder_thinking_active(threshold) is True
    settings = coder_model_settings(attempt=threshold)
    assert settings["extra_body"]["chat_template_kwargs"] == {"enable_thinking": True}
    assert settings.get("timeout") == get_settings().coder_thinking_timeout_s
    assert settings.get("timeout") > get_settings().coder_request_timeout_s


def test_thinking_disabled_by_flag(monkeypatch) -> None:
    monkeypatch.setenv("CODER_THINKING_ENABLED", "false")
    _reset_settings()
    try:
        assert coder_thinking_active(99) is False
        settings = coder_model_settings(attempt=99)
        assert "chat_template_kwargs" not in settings.get("extra_body", {})
    finally:
        get_settings.cache_clear()


def test_pydantic_ai_model_applies_explicit_work_deadline(monkeypatch) -> None:
    monkeypatch.setenv("WORK_REQUEST_TIMEOUT_S", "123")
    monkeypatch.setenv("MODEL_MAX_RETRIES", "1")
    _reset_settings()
    try:
        client = pydantic_ai_model(Role.WORK).client
        assert client.timeout == 123
        assert client.max_retries == 1
    finally:
        get_settings.cache_clear()


def test_pydantic_ai_model_gives_coder_lane_its_own_deadline(monkeypatch) -> None:
    monkeypatch.setenv("WORK_REQUEST_TIMEOUT_S", "123")
    monkeypatch.setenv("CODER_REQUEST_TIMEOUT_S", "456")
    _reset_settings()
    try:
        assert pydantic_ai_model(Role.CODING).client.timeout == 456
    finally:
        get_settings.cache_clear()


def test_refusal_loop_stops_even_when_the_calls_are_not_identical() -> None:
    """Varying a refused call slightly is the common shape, and it used to run forever.

    One run spent ~30 minutes cycling `cd X && pytest`, `pytest 2>&1 | head`, `ls`, `cp`
    against the shell-operator and allowlist rules — never three identical in a row, so
    the identical-call detector never fired.
    """
    refusals = [
        {"tool": "run_command", "args": {"command": cmd}, "ok": False}
        for cmd in (
            "cd /w && pytest -q",
            "cd /w && pytest -q 2>&1",
            "pytest -q 2>&1 | head -40",
            "ls -la /w",
            "cp overlays/clean/conftest.py tests/",
            "PYTHONPATH=src python -c 'import platform'",
        )
    ]
    assert _tool_log_has_repeated_call(refusals) is False
    assert _tool_log_is_stuck_on_refusals(refusals) is True


def test_productive_tool_log_is_not_mistaken_for_a_refusal_loop() -> None:
    log = [{"tool": "read_file", "args": {"path": f"a{i}.py"}, "ok": True} for i in range(8)]
    log.append({"tool": "run_command", "args": {"command": "ls"}, "ok": False})
    assert _tool_log_is_stuck_on_refusals(log) is False
