from __future__ import annotations

from pydantic_ai.models.openai import OpenAIChatModel, OpenAIChatModelSettings
from pydantic_ai.providers.openai import OpenAIProvider

from sprint_crew.config import Role, get_settings, lane_for_role


def pydantic_ai_model(role: Role) -> OpenAIChatModel:
    lane = lane_for_role(role)
    provider = OpenAIProvider(base_url=lane.base_url, api_key="local")
    return OpenAIChatModel(lane.served_name, provider=provider)


def coder_thinking_active(attempt: int) -> bool:
    """True when reasoning escalation should engage for this coder attempt."""
    settings = get_settings()
    return settings.coder_thinking_enabled and attempt >= settings.coder_thinking_escalation_attempt


def coder_model_settings(*, attempt: int = 0) -> OpenAIChatModelSettings:
    """Laguna coder sampling for a given attempt.

    Sampling is pinned to Poolside's eval-certified values (top_k rides in
    extra_body — pydantic-ai does not map it for chat models). From
    ``coder_thinking_escalation_attempt`` onward we enable per-request thinking
    and swap to the longer timeout so the reasoning trace does not overrun the
    request deadline.
    """
    settings = get_settings()
    extra_body: dict[str, object] = {"top_k": settings.coder_top_k}
    thinking = coder_thinking_active(attempt)
    if thinking:
        extra_body["chat_template_kwargs"] = {"enable_thinking": True}
    timeout = settings.coder_thinking_timeout_s if thinking else settings.coder_request_timeout_s
    return OpenAIChatModelSettings(
        temperature=settings.coder_temperature,
        top_p=settings.coder_top_p,
        timeout=timeout,
        extra_body=extra_body,
    )
