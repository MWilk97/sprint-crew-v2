"""Shared construction for the three tool-loop agents (Coder, TechLead, Tester).

All three built the same five-argument ``Agent`` by hand; only Coder had wrapped it in a
named helper, so the two others drifted into inline copies. Keeping one factory makes the
real per-agent differences — role, prompt, toolset, retry budget, sampling — the only thing
each call site states.

Structured-output agents (Reviewer, Formatter, Interpreter, ScrumMaster) do not belong here:
they never build an ``Agent`` at all, going through ``inference.structured`` instead.
"""

from __future__ import annotations

from pydantic_ai import Agent
from pydantic_ai.settings import ModelSettings
from pydantic_ai.toolsets import AbstractToolset

from sprint_crew.config import Role
from sprint_crew.inference.router import pydantic_ai_model
from sprint_crew.tools.pydantic_ai import WorkspaceDeps


def build_tool_agent(
    role: Role,
    *,
    system_prompt: str,
    toolset: AbstractToolset[WorkspaceDeps],
    retries: int,
    model_settings: ModelSettings | None = None,
) -> Agent[WorkspaceDeps, str]:
    agent: Agent[WorkspaceDeps, str] = Agent(
        pydantic_ai_model(role),
        deps_type=WorkspaceDeps,
        system_prompt=system_prompt,
        toolsets=[toolset],
        retries=retries,
        model_settings=model_settings,
    )
    return agent
