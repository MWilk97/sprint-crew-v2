"""Explainer agent — answers a question about the repo, changes nothing (roadmap M9).

The mechanism is entirely pre-existing: ``build_readonly_toolset(include_semantic_search=True)``
plus ``workspace_deps(mutate=False)`` has been generic since day one, and the TechLead was
simply its only caller. What is new here is that the answer *streams*, and that its citations
are derived rather than requested.

**Streaming.** The roadmap rules token-level streaming out of scope, and that ruling is about
``structured_completion``: it uses guided JSON with a repair-retry loop, so streaming it would
mean reworking parsing for every reporter agent. This agent produces prose through a plain
pydantic-ai ``Agent``, where the constraint does not apply. Deltas are gated on
``FinalResultEvent`` so the tool-calling turns in the middle of a run do not leak their
scratch text into the answer bubble.

**``answer_complete`` is authoritative, deltas are a preview.** ``run.result.output`` is the
answer; the streamed text is what reached the client first. They can differ — a model that
emits text and then decides to call another tool has already streamed a paragraph that is not
the final answer. A client renders deltas as they arrive and then replaces the bubble with the
completed text, which is also what makes an SSE reconnect (replayed deltas) idempotent.

**Citations.** Derived from the tool log and the answer's own inline ``path:line``
references, never asked of the model: the files it opened are a fact, whereas a model asked
to cite itself invents line numbers.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pydantic_ai import Agent
from pydantic_ai.exceptions import ModelAPIError, UsageLimitExceeded
from pydantic_ai.messages import FinalResultEvent, PartDeltaEvent, TextPartDelta
from pydantic_ai.run import AgentRun
from pydantic_ai.settings import ModelSettings
from pydantic_ai.usage import UsageLimits

from sprint_crew.agents._factory import build_tool_agent
from sprint_crew.agents.prompts_explainer import (
    build_explainer_system_prompt,
    build_explainer_user_prompt,
)
from sprint_crew.agents.tool_events import ToolCallLog
from sprint_crew.config import Role, get_settings
from sprint_crew.paths import paths_in_text
from sprint_crew.schemas.console import AnswerCitation
from sprint_crew.tools.pydantic_ai import WorkspaceDeps, build_readonly_toolset, workspace_deps

logger = logging.getLogger(__name__)

#: How many citations one answer may carry. A turn that reads twenty files is exploring,
#: not citing, and a UI cannot render twenty links usefully either.
_MAX_CITATIONS = 12

#: ``src/foo.py:120`` inside the prose — the model's own pointer at a line.
_INLINE_REF_RE = re.compile(
    r"([\w./-]+\.(?:py|js|ts|tsx|jsx|go|rs|md|yaml|yml|json|toml|txt|sh)):(\d+)",
    re.IGNORECASE,
)

#: ``path:start-end`` at the head of a formatted semantic_search hit (vector/search.py).
_HIT_RE = re.compile(r"^([\w./-]+):(\d+)-(\d+) \(score=", re.MULTILINE)


@dataclass(frozen=True)
class ExplainerAnswer:
    text: str
    citations: list[AnswerCitation]
    tool_calls: int
    #: The turn budget or the wall clock ran out before the model finished.
    truncated: bool = False


class DeltaBuffer:
    """Coalesces token deltas into flushes worth one event each.

    One event per token would be one SQLite row and one bus publish per token. Flushing on
    whichever of size or age trips first keeps a fast answer cheap without letting a slow
    one sit unflushed — a model producing a token a second must still look alive.
    """

    def __init__(
        self,
        on_flush: Callable[[str], None],
        *,
        max_chars: int,
        interval_s: float,
        now: Callable[[], float] = time.monotonic,
    ) -> None:
        self._on_flush = on_flush
        self._max_chars = max(1, max_chars)
        self._interval_s = max(0.0, interval_s)
        self._now = now
        self._buffer: list[str] = []
        self._size = 0
        self._last_flush = now()

    def add(self, text: str) -> None:
        if not text:
            return
        self._buffer.append(text)
        self._size += len(text)
        if self._size >= self._max_chars or (self._now() - self._last_flush) >= self._interval_s:
            self.flush()

    def flush(self) -> None:
        if not self._buffer:
            return
        payload = "".join(self._buffer)
        self._buffer.clear()
        self._size = 0
        self._last_flush = self._now()
        self._on_flush(payload)


@dataclass
class _Evidence:
    """What the tool log says the agent actually looked at, in call order."""

    opened: dict[str, tuple[int | None, int | None]] = field(default_factory=dict)
    searched: dict[str, tuple[int | None, int | None]] = field(default_factory=dict)


def _collect_evidence(tool_log: ToolCallLog) -> _Evidence:
    evidence = _Evidence()
    for entry in tool_log:
        if not entry.get("ok"):
            continue
        tool = entry.get("tool")
        args = entry.get("args") or {}
        if tool == "read_file" and (path := args.get("path")):
            # A later read of the same file wins: it is usually the narrowing one.
            evidence.opened[str(path).lstrip("./")] = (args.get("start_line"), args.get("end_line"))
        elif tool == "semantic_search":
            # output_preview is capped at 500 chars, so this recovers the first hit or two —
            # enough for the fallback, which is all searched hits are used for.
            for path, start, end in _HIT_RE.findall(str(entry.get("output_preview") or "")):
                evidence.searched.setdefault(path.lstrip("./"), (int(start), int(end)))
    return evidence


def _inline_refs(answer: str) -> dict[str, int]:
    """First ``path:line`` the answer states for each path."""
    refs: dict[str, int] = {}
    for path, line in _INLINE_REF_RE.findall(answer):
        refs.setdefault(path.lstrip("./"), int(line))
    return refs


def _cite(path: str, evidence: _Evidence, inline_line: int | None) -> AnswerCitation:
    """One citation for a path the answer named, attributed to the strongest evidence."""
    if path in evidence.opened:
        start, end = evidence.opened[path]
        source = "read_file"
    elif path in evidence.searched:
        start, end = evidence.searched[path]
        source = "semantic_search"
    else:
        start, end, source = None, None, "answer"
    # An inline path:line is the model pointing at the exact line; it beats the range of
    # whatever read_file call happened to fetch it.
    if inline_line is not None:
        start, end = inline_line, None
    return AnswerCitation(path=path, start_line=start, end_line=end, source=source)


def _fallback_citations(evidence: _Evidence, limit: int) -> list[AnswerCitation]:
    """What an answer that cites nothing was nonetheless based on."""
    citations: list[AnswerCitation] = []
    seen: set[str] = set()
    for source, found in (("read_file", evidence.opened), ("semantic_search", evidence.searched)):
        for path, (start, end) in found.items():
            if path in seen:
                continue
            seen.add(path)
            citations.append(
                AnswerCitation(path=path, start_line=start, end_line=end, source=source)
            )
            if len(citations) >= limit:
                return citations
    return citations


def derive_citations(
    answer: str,
    tool_log: ToolCallLog,
    *,
    workspace_root: Path | None = None,
    limit: int = _MAX_CITATIONS,
) -> list[AnswerCitation]:
    """Where the answer came from, in the order the answer refers to it.

    Paths the answer names are preferred — they are what the reader wants to click — and are
    kept only when the agent actually touched them or they exist on disk, so a hallucinated
    path never becomes a broken link. An answer that cites nothing falls back to the files
    the agent opened, which is still a truthful "here is what this was based on".
    """
    evidence = _collect_evidence(tool_log)
    refs = _inline_refs(answer)

    def grounded(path: str) -> bool:
        if path in evidence.opened or path in evidence.searched:
            return True
        return workspace_root is not None and (workspace_root / path).is_file()

    named = [p for p in paths_in_text(answer) if grounded(p)]
    citations = [_cite(path, evidence, refs.get(path)) for path in named[:limit]]
    return citations or _fallback_citations(evidence, limit)


async def _stream_node(
    node: Any, run: AgentRun[WorkspaceDeps, str], take: Callable[[str], None]
) -> None:
    """Forward one model turn's final-result text deltas to ``take``.

    Gated on ``FinalResultEvent`` and reset per node: only the turn that produces the final
    result is the answer, and an earlier turn's text is scratch work on the way to a tool
    call — streaming it would put the model's deliberation in the user's answer bubble.

    ``node`` is untyped because its only public name lives in ``pydantic_ai._agent_graph``;
    the caller has already narrowed it with ``Agent.is_model_request_node``.
    """
    async with node.stream(run.ctx) as request_stream:
        final = False
        async for event in request_stream:
            if isinstance(event, FinalResultEvent):
                final = True
            elif (
                final
                and isinstance(event, PartDeltaEvent)
                and isinstance(event.delta, TextPartDelta)
            ):
                take(event.delta.content_delta)


async def run_explainer(
    *,
    question: str,
    workspace_root: Path,
    session_id: str,
    conversation: str = "",
    on_delta: Callable[[str], None] | None = None,
    tool_call_log: ToolCallLog | None = None,
) -> ExplainerAnswer:
    """Answer ``question`` from the repository at ``workspace_root``. Never writes."""
    settings = get_settings()
    root = workspace_root.resolve()
    log: ToolCallLog = tool_call_log if tool_call_log is not None else []

    deps = workspace_deps(
        root,
        mutate=False,
        session_id=session_id,
        event_agent="explainer",
        include_semantic_search=True,
        # The session's checkout is the repository's committed state, so the shared repo
        # index is the whole truth for it; there is no run overlay to layer on top.
        index_overlay=False,
        tool_call_log=log,
    )
    agent = build_tool_agent(
        Role.WORK,
        system_prompt=build_explainer_system_prompt(max_turns=settings.max_explainer_turns),
        toolset=build_readonly_toolset(include_semantic_search=True),
        model_settings=ModelSettings(temperature=0),
    )
    prompt = build_explainer_user_prompt(question=question, conversation=conversation)

    buffer = DeltaBuffer(
        on_delta or (lambda _text: None),
        max_chars=settings.answer_delta_chars,
        interval_s=settings.answer_delta_interval_s,
    )
    streamed: list[str] = []
    truncated = False
    output = ""

    def take(chunk: str) -> None:
        streamed.append(chunk)
        buffer.add(chunk)

    try:
        async with asyncio.timeout(settings.explainer_timeout_s):
            async with agent.iter(
                prompt,
                deps=deps,
                usage_limits=UsageLimits(request_limit=settings.max_explainer_turns),
                model_settings=ModelSettings(temperature=0),
            ) as run:
                async for node in run:
                    if Agent.is_model_request_node(node):
                        await _stream_node(node, run, take)
                if run.result is not None:
                    output = (run.result.output or "").strip()
    except UsageLimitExceeded:
        logger.info("explainer hit the %s-turn budget", settings.max_explainer_turns)
        truncated = True
    except TimeoutError:
        logger.info("explainer exceeded %ss", settings.explainer_timeout_s)
        truncated = True
    except ModelAPIError as exc:
        # Partial text is already on the client's screen, so returning what there is beats
        # raising and leaving a half-written bubble with no ending.
        logger.warning("explainer model request failed: %s", exc)
        truncated = True
    finally:
        buffer.flush()

    if not output:
        output = "".join(streamed).strip()
    if not output:
        output = (
            "I could not produce an answer from this repository within the turn budget."
            if truncated
            else "I could not find an answer to that in this repository."
        )

    return ExplainerAnswer(
        text=output,
        citations=derive_citations(output, log, workspace_root=root),
        tool_calls=len(log),
        truncated=truncated,
    )
