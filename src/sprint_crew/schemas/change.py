from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

_STRICT = ConfigDict(extra="forbid")

FileAction = Literal["created", "modified", "deleted"]
ReviewSeverity = Literal["blocker", "warning", "nit"]


class FileChange(BaseModel):
    model_config = _STRICT

    path: str = Field(..., min_length=1)
    action: FileAction
    summary: str = Field(..., min_length=1)


class CodeChange(BaseModel):
    model_config = _STRICT

    ticket_key: str = Field(..., min_length=1)
    branch: str = Field(..., min_length=1)
    summary: str = Field(..., min_length=1)
    files_changed: list[FileChange] = Field(default_factory=list)
    tests_added: list[str] = Field(default_factory=list)
    tests_passed: bool
    notes: str = Field(default="")


class TestAdditions(BaseModel):
    __test__ = False
    model_config = _STRICT

    ticket_key: str = Field(..., min_length=1)
    tests_added: list[str] = Field(default_factory=list)
    coverage_summary: str = Field(..., min_length=1)
    tests_passed: bool
    bugs_observed: str = Field(default="")


class ReviewFinding(BaseModel):
    model_config = _STRICT

    severity: ReviewSeverity
    file: str = Field(default="")
    line: int | None = Field(default=None, ge=1)
    message: str = Field(..., min_length=1)

    @field_validator("line", mode="before")
    @classmethod
    def _coerce_zero_to_none(cls, value: Any) -> Any:
        if value == 0:
            return None
        return value


class ReviewOutcome(BaseModel):
    model_config = _STRICT

    ticket_key: str = Field(..., min_length=1)
    passed: bool
    summary: str = Field(..., min_length=1)
    findings: list[ReviewFinding] = Field(default_factory=list)
    tests_run: list[str] = Field(default_factory=list)
    tests_passed: bool


class SecurityReviewOutcome(ReviewOutcome):
    model_config = _STRICT


class ReviewConsensus(BaseModel):
    model_config = _STRICT

    reviews: list[ReviewOutcome] = Field(..., min_length=1)
    canonical: ReviewOutcome
    agreed: bool
