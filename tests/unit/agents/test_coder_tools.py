from __future__ import annotations

import pytest
from pydantic_ai.exceptions import ModelAPIError

from sprint_crew.agents.coder import _tool_log_has_repeated_call
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
