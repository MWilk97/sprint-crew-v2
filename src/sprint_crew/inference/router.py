from __future__ import annotations

from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.openai import OpenAIProvider

from sprint_crew.config import Role, lane_for_role


def pydantic_ai_model(role: Role) -> OpenAIChatModel:
    lane = lane_for_role(role)
    provider = OpenAIProvider(base_url=lane.base_url, api_key="local")
    return OpenAIChatModel(lane.served_name, provider=provider)


def served_model_name(role: Role) -> str:
    return lane_for_role(role).served_name
