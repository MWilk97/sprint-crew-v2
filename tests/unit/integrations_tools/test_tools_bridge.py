from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from sprint_crew.tools import build_registry
from sprint_crew.tools.base import AsyncTool, ToolResult
from sprint_crew.tools.pydantic_ai import build_coder_toolset, workspace_deps


def test_workspace_deps_registry(tmp_path: Path) -> None:
    deps = workspace_deps(tmp_path, mutate=True)
    assert deps.root == tmp_path.resolve()
    assert deps.registry.get("read_file") is not None


def test_coder_toolset_builds() -> None:
    ts = build_coder_toolset()
    assert ts is not None


def test_read_file_via_registry(tmp_path: Path) -> None:
    target = tmp_path / "hello.txt"
    target.write_text("hi", encoding="utf-8")
    reg = build_registry()
    result = reg.dispatch("read_file", {"path": "hello.txt"}, workspace_root=tmp_path)
    assert result.ok
    assert "hi" in result.output


def test_run_command_declares_an_async_body() -> None:
    """run_command's child can live 300 s, so it must not be dispatched into a thread —
    a thread is not cancellable and the child would survive Stop.
    """
    assert isinstance(build_registry().get("run_command"), AsyncTool)


@pytest.mark.asyncio
async def test_adispatch_prefers_the_async_body_over_a_thread(tmp_path: Path) -> None:
    reg = build_registry()

    async def _aexecute(args, *, workspace_root):
        return ToolResult(ok=True, output="async path")

    with (
        patch.object(reg.get("run_command"), "aexecute", _aexecute),
        patch("asyncio.to_thread") as to_thread,
    ):
        result = await reg.adispatch(
            "run_command", {"command": "pytest -q"}, workspace_root=tmp_path
        )

    assert result.output == "async path"
    to_thread.assert_not_called()


@pytest.mark.asyncio
async def test_adispatch_threads_tools_without_an_async_body(tmp_path: Path) -> None:
    """grep and the git tools stay synchronous on purpose; they must still leave the loop."""
    target = tmp_path / "hello.txt"
    target.write_text("hi", encoding="utf-8")
    reg = build_registry()

    result = await reg.adispatch("read_file", {"path": "hello.txt"}, workspace_root=tmp_path)

    assert result.ok
    assert "hi" in result.output


@pytest.mark.asyncio
async def test_adispatch_reports_unknown_tools_like_dispatch(tmp_path: Path) -> None:
    reg = build_registry()
    sync = reg.dispatch("nope", {}, workspace_root=tmp_path)
    api = await reg.adispatch("nope", {}, workspace_root=tmp_path)
    assert (sync.ok, sync.error) == (api.ok, api.error) == (False, "unknown tool")
