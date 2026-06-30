from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

_STRICT = ConfigDict(extra="forbid")


class JiraTicket(BaseModel):
    model_config = _STRICT

    key: str = Field(..., min_length=1)
    summary: str = Field(..., min_length=1)
    description: str = Field(default="")
    status: str = Field(..., min_length=1)
    issue_type: str = Field(..., min_length=1)
    url: str = Field(default="")
    assignee: str | None = Field(default=None)
    acceptance_criteria: str = Field(default="")


class TicketSelection(BaseModel):
    model_config = _STRICT

    ticket: JiraTicket
    sprint_id: str = Field(default="")
    rationale: str = Field(..., min_length=1)
    risk_flags: list[str] = Field(default_factory=list)


class PlanStep(BaseModel):
    model_config = _STRICT

    description: str = Field(..., min_length=1)
    files: list[str] = Field(default_factory=list)


class TaskPlan(BaseModel):
    model_config = _STRICT

    ticket_key: str = Field(..., min_length=1)
    summary: str = Field(..., min_length=1)
    steps: list[PlanStep] = Field(..., min_length=1)
    files_to_touch: list[str] = Field(default_factory=list)
    acceptance_tests: list[str] = Field(..., min_length=1)
    out_of_scope: list[str] = Field(default_factory=list)
