"""Interpreter agent contract — the LLM half of the clarify step (ADR 0013).

Kept separate from schemas/console.py because these are *model output* shapes, not
API shapes. The model never invents ids: it returns ordered questions/options and a
``recommended_index``, and Python assigns stable ``question_id``/``suggestion_id``
values in ``sprint_crew.agents.interpreter``. Ids from an LLM drift between retries
and collide across questions; indices are checkable.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from sprint_crew.schemas.console import IntentSummary

_STRICT = ConfigDict(extra="forbid")


class InterpreterOption(BaseModel):
    """One answer the user can pick for a clarify question."""

    model_config = _STRICT

    label: str = Field(..., min_length=1)
    detail: str | None = None
    rationale: str | None = None


class InterpreterQuestion(BaseModel):
    model_config = _STRICT

    text: str = Field(..., min_length=1)
    why_asked: str = Field(..., min_length=1)
    options: list[InterpreterOption] = Field(..., min_length=2, max_length=4)
    # Index into ``options``. Out-of-range values are clamped at conversion time
    # rather than raising — a bad recommendation must not sink the whole clarify step.
    recommended_index: int = Field(default=0, ge=0)
    allow_custom: bool = True


class IntentAnalysis(IntentSummary):
    """What the Interpreter understood, and what it still needs to know.

    Extends the API-facing summary so the shared fields cannot drift apart: the console
    surfaces everything here except the questions, which it maps to ClarifyQuestion.
    """

    model_config = _STRICT

    # Empty means the request is already actionable — the session skips straight to ready.
    questions: list[InterpreterQuestion] = Field(default_factory=list)
