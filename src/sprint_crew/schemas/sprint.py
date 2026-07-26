"""Request/response bodies for the /sprint/* routes.

These lived inline in ``api/app.py`` and were the only API models in the codebase without
``extra="forbid"`` — see ``_base``.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from sprint_crew.schemas._base import STRICT


class FromPromptRequest(BaseModel):
    model_config = STRICT

    prompt: str = Field(..., min_length=1)
    repo_url: str | None = None


class FromTicketRequest(BaseModel):
    model_config = STRICT

    ticket_key: str = Field(..., min_length=1)
    repo_url: str | None = None


class SessionCreatedResponse(BaseModel):
    model_config = STRICT

    session_id: str


class BacklogRunCreatedResponse(BaseModel):
    model_config = STRICT

    run_id: str
