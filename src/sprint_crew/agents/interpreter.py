"""Interpreter agent — turns a raw user request into clarify questions (ADR 0013).

Runs on the Work lane before any planning. Structured-output only: no tools, no repo
writes. The model returns ordered questions; ids and the recommendation pointer are
resolved here so they stay stable and self-consistent.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence

from sprint_crew.agents.prompts_interpreter import (
    build_interpreter_system_prompt,
    build_interpreter_user_content,
    build_interpreter_user_prompt,
)
from sprint_crew.config import Role, get_settings, lane_for_role
from sprint_crew.inference.router import thinking_chat_template_kwargs
from sprint_crew.inference.structured import structured_completion
from sprint_crew.schemas.attachment import AttachmentPayload
from sprint_crew.schemas.console import ClarifyQuestion, ClarifySuggestion
from sprint_crew.schemas.intent import IntentAnalysis


def to_clarify_questions(
    analysis: IntentAnalysis, *, limit: int, round_no: int = 1
) -> list[ClarifyQuestion]:
    """Map model output to API questions, assigning ids and resolving recommendations.

    Ids carry ``round_no`` because a later message re-interprets and replaces the whole set
    (roadmap M9). Without it, round 2's first question would be ``q-1`` again and would
    silently inherit round 1's answer.
    """
    questions: list[ClarifyQuestion] = []
    for q_index, question in enumerate(analysis.questions[:limit], start=1):
        suggestions = [
            ClarifySuggestion(
                suggestion_id=f"s-{round_no}-{q_index}-{o_index}",
                label=option.label,
                detail=option.detail,
                rationale=option.rationale,
            )
            for o_index, option in enumerate(question.options, start=1)
        ]
        # Clamp rather than raise: an out-of-range pick costs a recommendation, not the run.
        recommended = suggestions[min(question.recommended_index, len(suggestions) - 1)]
        questions.append(
            ClarifyQuestion(
                question_id=f"q-{round_no}-{q_index}",
                text=question.text,
                why_asked=question.why_asked,
                suggestions=suggestions,
                allow_custom=question.allow_custom,
                recommended_suggestion_id=recommended.suggestion_id,
            )
        )
    return questions


def _visible_attachments(
    lane: Role, attachments: Sequence[AttachmentPayload]
) -> list[AttachmentPayload]:
    """Drop image data the lane cannot look at, keeping the attachment itself announced.

    Clarify degrades, never blocks (ADR 0013): against a text-only Work model — the
    documented ``qwen3-30b-a3b-thinking`` rollback — an attached screenshot becomes a named
    image the Interpreter knows it was given and cannot see, rather than a failed request.
    """
    if lane_for_role(lane).supports_vision:
        return list(attachments)
    return [a.model_copy(update={"data_url": ""}) for a in attachments]


async def run_interpreter(
    *,
    user_prompt: str,
    repo_context: str = "",
    conversation: str = "",
    project_hint: str = "",
    attachments: Sequence[AttachmentPayload] = (),
    role: Role | None = None,
    thinking: bool | None = None,
) -> IntentAnalysis:
    settings = get_settings()
    lane = role or Role.WORK
    visible = _visible_attachments(lane, attachments)
    enable_thinking = settings.interpreter_thinking_enabled if thinking is None else thinking
    # A reasoning trace does not fit the interactive deadline — measured at 177s vs 15s on
    # GX10 (probe E), well past the default timeout. Same invariant as the Coder lane's
    # escalation in inference/router.py: thinking implies the longer deadline.
    timeout = (
        settings.interpreter_thinking_timeout_s
        if enable_thinking
        else settings.interpreter_timeout_s
    )
    return await asyncio.to_thread(
        structured_completion,
        lane,
        system_prompt=build_interpreter_system_prompt(max_questions=settings.max_clarify_questions),
        user_prompt=build_interpreter_user_content(
            build_interpreter_user_prompt(
                user_prompt=user_prompt,
                repo_context=repo_context,
                conversation=conversation,
                project_hint=project_hint,
                attachments=visible,
            ),
            visible,
        ),
        output_type=IntentAnalysis,
        timeout_seconds=timeout,
        extra_body=thinking_chat_template_kwargs(enable_thinking),
    )
