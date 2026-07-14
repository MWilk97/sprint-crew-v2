from __future__ import annotations

import re
from enum import Enum
from typing import Literal

from sprint_crew.paths import paths_in_text
from sprint_crew.schemas.ticket import JiraTicket

_COMPLEX_KEYWORDS = re.compile(
    r"\b(?:REST|RESTful\s+API|API\s+(?:endpoint|endpoints|routes?|server|integration|layer|gateway)|"
    r"CRUD|auth|authentication|authorization|migration|database|"
    r"sqlite|postgres|microservice|refactor|architecture|integrat(?:e|ion)|"
    r"endpoint(?:s)?|middleware|deploy(?:ment)?|openapi|swagger|graphql|"
    r"websocket|queue|cache|redis|kubernetes|docker(?:ize)?)\b",
    re.IGNORECASE,
)

_MULTI_FEATURE_MARKERS = re.compile(
    r"\b(?:and also|as well as|in addition|split into|multiple|several|"
    r"full stack|end[- ]to[- ]end)\b",
    re.IGNORECASE,
)

_TRIVIAL_ACTION_RE = re.compile(
    r"\b(?:add|implement|introduce|create)\s+(?:a\s+)?(?:\w+\s+){0,3}"
    r"(?:function|method|helper|test|tests|pytest|hello|greet(?:er)?)\b",
    re.IGNORECASE,
)

_FIX_TRIVIAL_RE = re.compile(
    r"\bfix\s+(?:a\s+)?(?:bug|typo|test|failing)\b",
    re.IGNORECASE,
)


class PromptComplexity(str, Enum):
    TRIVIAL = "trivial"
    SIMPLE = "simple"
    COMPLEX = "complex"


def assess_prompt_complexity(prompt: str) -> PromptComplexity:
    """Heuristic prompt complexity for backlog model routing."""
    text = prompt.strip()
    if not text:
        return PromptComplexity.SIMPLE

    lowered = text.lower()
    if _COMPLEX_KEYWORDS.search(text) or _MULTI_FEATURE_MARKERS.search(text):
        return PromptComplexity.COMPLEX

    if len(text) > 450 or text.count("\n") >= 4:
        return PromptComplexity.COMPLEX

    paths = paths_in_text(text)
    if len(paths) > 3:
        return PromptComplexity.COMPLEX

    if (
        len(text) <= 280
        and len(paths) <= 2
        and (_TRIVIAL_ACTION_RE.search(text) or _FIX_TRIVIAL_RE.search(text))
        and lowered.count(" and ") <= 1
    ):
        return PromptComplexity.TRIVIAL

    if len(text) <= 180 and len(paths) <= 1 and "pytest" in lowered:
        return PromptComplexity.TRIVIAL

    return PromptComplexity.SIMPLE


def assess_ticket_complexity(ticket: JiraTicket) -> PromptComplexity:
    """Heuristic ticket complexity for TechLead mode selection."""
    text = "\n".join(
        [
            ticket.summary,
            ticket.description,
            ticket.acceptance_criteria,
        ]
    ).strip()
    if not text:
        return PromptComplexity.SIMPLE
    if ticket.issue_type.lower() in {"epic", "initiative"}:
        return PromptComplexity.COMPLEX
    return assess_prompt_complexity(text)


def tech_lead_mode(ticket: JiraTicket) -> Literal["static", "tool_loop"]:
    """Return tool_loop only for COMPLEX tickets; static for TRIVIAL/SIMPLE."""
    if assess_ticket_complexity(ticket) == PromptComplexity.COMPLEX:
        return "tool_loop"
    return "static"
