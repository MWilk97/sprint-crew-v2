"""Shared bearer-token gate for console and /sprint routes."""

from __future__ import annotations

from fastapi import Header, HTTPException

from sprint_crew.config import get_settings


async def require_token(authorization: str | None = Header(default=None)) -> None:
    """Require ``Authorization: Bearer <CONSOLE_API_TOKEN>`` when the token is set.

    Empty ``CONSOLE_API_TOKEN`` disables auth (unit tests, smoke_cycle).
    """
    token = get_settings().console_api_token
    if not token:
        return
    if authorization != f"Bearer {token}":
        raise HTTPException(status_code=401, detail="Unauthorized")
