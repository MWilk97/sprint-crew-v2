from __future__ import annotations

from sprint_crew.tools.apply_patch import apply_patch_tool
from sprint_crew.tools.base import Tool, ToolRegistry
from sprint_crew.tools.git_tools import git_diff_tool, git_log_tool, git_status_tool
from sprint_crew.tools.grep import grep_tool
from sprint_crew.tools.list_directory import list_directory_tool
from sprint_crew.tools.read_file import read_file_tool
from sprint_crew.tools.run_command import run_command_tool
from sprint_crew.tools.write_file import write_file_tool

ALL_TOOLS: list[Tool] = [
    read_file_tool,
    write_file_tool,
    apply_patch_tool,
    grep_tool,
    list_directory_tool,
    run_command_tool,
    git_status_tool,
    git_diff_tool,
    git_log_tool,
]

READONLY_TOOLS: list[Tool] = [
    read_file_tool,
    grep_tool,
    list_directory_tool,
    run_command_tool,
    git_status_tool,
    git_diff_tool,
    git_log_tool,
]

MUTATE_TOOLS: list[Tool] = ALL_TOOLS


def build_registry(tools: list[Tool] | None = None) -> ToolRegistry:
    registry = ToolRegistry()
    for tool in tools or ALL_TOOLS:
        registry.register(tool)
    return registry
