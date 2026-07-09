from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, ConfigDict, Field

_STRICT = ConfigDict(extra="forbid")


class ProjectMode(str, Enum):
    GREENFIELD = "greenfield"
    BROWNFIELD = "brownfield"


class BacklogIssueType(str, Enum):
    STORY = "story"
    TASK = "task"
    BUG = "bug"
    EPIC = "epic"


class ProductBrief(BaseModel):
    model_config = _STRICT

    title: str = Field(..., min_length=1)
    summary: str = Field(..., min_length=1)
    goals: list[str] = Field(default_factory=list)
    constraints: list[str] = Field(default_factory=list)
    out_of_scope: list[str] = Field(default_factory=list)


class BacklogStory(BaseModel):
    model_config = _STRICT

    key: str = Field(..., min_length=1)
    summary: str = Field(..., min_length=1)
    description: str = Field(default="")
    issue_type: BacklogIssueType = BacklogIssueType.STORY
    acceptance_criteria: str = Field(default="")
    priority: int = Field(default=0, ge=0)
    depends_on: list[str] = Field(default_factory=list)


class BacklogPlan(BaseModel):
    model_config = _STRICT

    product_brief: ProductBrief
    stories: list[BacklogStory] = Field(..., min_length=1)
    recommended_first: str = Field(..., min_length=1)
    project_mode: ProjectMode = ProjectMode.BROWNFIELD
