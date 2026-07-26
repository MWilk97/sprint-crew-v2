"""Shared bearer-token gate for console and /sprint routes."""

from __future__ import annotations

from fastapi import Header, HTTPException, Query

from sprint_crew.config import get_settings


async def require_token(
    authorization: str | None = Header(default=None),
    token: str | None = Query(default=None),
) -> None:
    """Require ``Authorization: Bearer <CONSOLE_API_TOKEN>`` when the token is set.

    Empty ``CONSOLE_API_TOKEN`` disables auth (unit tests, smoke_cycle).

    The ``?token=`` query fallback exists for the SSE stream: browser ``EventSource``
    cannot set an ``Authorization`` header, so the FE passes the token in the query string
    on ``/stream`` (M3). It is accepted on every route for simplicity, but the header is the
    intended path everywhere else — a query-string token can surface in access logs.
    """
    expected = get_settings().console_api_token
    if not expected:
        return
    if authorization == f"Bearer {expected}" or token == expected:
        return
    raise HTTPException(status_code=401, detail="Unauthorized")
