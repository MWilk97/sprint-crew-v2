from __future__ import annotations

from openai import AsyncOpenAI
from pydantic_ai.models.openai import OpenAIChatModel, OpenAIChatModelSettings
from pydantic_ai.providers.openai import OpenAIProvider

from sprint_crew.config import Role, get_settings, lane_for_role


def pydantic_ai_model(role: Role) -> OpenAIChatModel:
    """Lane-bound model with an explicit request deadline.

    The provider's own default is a 600 s read with 2 SDK retries, which is both invisible
    and a 30-minute worst case per logical request. The Coder overrides the deadline per
    request (see ``coder_model_settings``); this is what every other role gets.
    """
    lane = lane_for_role(role)
    settings = get_settings()
    timeout = (
        settings.coder_request_timeout_s if role is Role.CODING else settings.work_request_timeout_s
    )
    client = AsyncOpenAI(
        base_url=lane.base_url,
        api_key="local",
        timeout=timeout,
        max_retries=settings.model_max_retries,
    )
    return OpenAIChatModel(lane.served_name, provider=OpenAIProvider(openai_client=client))


def thinking_chat_template_kwargs(enabled: bool) -> dict[str, object]:
    """vLLM knob that gates a model's reasoning trace.

    Sole spelling of this vendor detail — Qwen3.x and Laguna templates both branch on
    ``enable_thinking``, and callers should not re-type the nesting.
    """
    return {"chat_template_kwargs": {"enable_thinking": enabled}}


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
        extra_body.update(thinking_chat_template_kwargs(True))
    timeout = settings.coder_thinking_timeout_s if thinking else settings.coder_request_timeout_s
    return OpenAIChatModelSettings(
        temperature=settings.coder_temperature,
        top_p=settings.coder_top_p,
        timeout=timeout,
        extra_body=extra_body,
    )
