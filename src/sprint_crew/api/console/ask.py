"""Codebase chat: ask a question about the repo, get a streamed answer (roadmap M9).

No ticket, no branch, no PR — the Explainer is read-only and nothing it does reaches git.
The answer arrives on the *existing* SSE timeline rather than on a channel of its own: a
console session already has one ordered event stream, and splitting chat off it would make
"the agent read this file while answering" and "the agent read this file during a run" two
incompatible shapes for the same fact.

**Why an ask takes the run slot.** It is not queued for fairness. ``ensure_lane`` stops every
other lane before starting one, so a run admitted midway through an ask would pull the Work
lane out from under it and the answer would die with a connection error. Holding the single
permit is the cheapest correct fix, and it makes Stop work for free.

**Rejected while a run is live** (409, not queued). Single-user by decision, and a user who
asked a question expects an answer in seconds — parking it behind a forty-minute run would
be worse than saying no. See the roadmap's appendix question 2 for the warm-lane policy this
deliberately does not adopt.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from uuid import uuid4

from fastapi import HTTPException

from sprint_crew.agents.explainer import run_explainer
from sprint_crew.api.console.run_bridge import sync_ask_state
from sprint_crew.api.console.state import (
    _STARTED_STATUSES,
    _lock_for,
    load_session,
    require_session,
    router,
    touch,
)
from sprint_crew.config import Role, get_settings
from sprint_crew.graph.lanes import ensure_lane
from sprint_crew.orchestrator.emitter import Emitter, emit_live, reset_emitter, set_emitter
from sprint_crew.orchestrator.event_log import event_log
from sprint_crew.orchestrator.run_registry import RunCancelled, run_registry
from sprint_crew.schemas.console import (
    AnswerCitation,
    AskRequest,
    ConsoleFile,
    ConsoleMessage,
    ConsoleMessageRole,
    ConsoleSession,
    WorkspaceStatus,
)
from sprint_crew.schemas.session import agent_event
from sprint_crew.tools._safety import UnsafePathError, resolve_safe_path

logger = logging.getLogger(__name__)

#: How much earlier conversation the Explainer is shown. Enough for "and what about the
#: other one?" to resolve; not so much that a long session pushes the repo out of context.
_CONVERSATION_TURNS = 6
_CONVERSATION_CHARS = 4000


def _conversation(session: ConsoleSession) -> str:
    recent = session.messages[-_CONVERSATION_TURNS:]
    text = "\n\n".join(f"{m.role.value}: {m.content}" for m in recent)
    if len(text) > _CONVERSATION_CHARS:
        text = "…\n" + text[-_CONVERSATION_CHARS:]
    return text


def _reject_unless_askable(session: ConsoleSession) -> None:
    """Every reason an ask cannot run right now, as a 409 that names the reason."""
    if not get_settings().console_ask_enabled:
        raise HTTPException(status_code=409, detail="codebase chat is disabled on this server")
    if session.status in _STARTED_STATUSES:
        raise HTTPException(
            status_code=409,
            detail=(
                f"session is {session.status.value}; the Work lane belongs to the run "
                "until it finishes"
            ),
        )
    # No check on terminal status: a finished session whose checkout is still on disk is
    # exactly when "what did it actually change?" gets asked.
    if session.workspace_status is not WorkspaceStatus.READY or not session.workspace_root:
        raise HTTPException(
            status_code=409,
            detail=f"repository is {session.workspace_status.value}; ask again when it is ready",
        )
    if session.ask_in_flight:
        raise HTTPException(status_code=409, detail="an answer is already being generated")
    if (active := run_registry().active_run_id()) is not None:
        raise HTTPException(
            status_code=409,
            detail=f"run {active} holds the Work lane; ask again when it finishes",
        )


@router.post("/sessions/{id}/ask", response_model=ConsoleSession, status_code=202)
async def ask_console_session(id: str, body: AskRequest) -> ConsoleSession:
    """Queue one Explainer turn and return 202. The answer arrives on the event stream.

    Accepted in every state that is not a live run: a finished session with its checkout
    still on disk is exactly when "what did it actually change?" gets asked.
    """
    await require_session(id)
    async with _lock_for(id):
        session = await require_session(id)
        await sync_ask_state(session)
        _reject_unless_askable(session)

        question = ConsoleMessage(role=ConsoleMessageRole.USER, content=body.question)
        session.messages.append(question)
        # Allocated here, not in the body: every answer_delta carries it, so the client must
        # learn the id from this response rather than from the first frame it happens to see.
        answer_id = f"m-{uuid4().hex[:8]}"
        ask_id = f"ask-{uuid4().hex[:8]}"
        session.active_ask_id = ask_id
        session.ask_in_flight = True
        conversation = _conversation(session)
        workspace_root = Path(session.workspace_root or "")
        await touch(session)

    run_registry().submit(
        ask_id,
        lambda: _answer(
            session_id=id,
            ask_id=ask_id,
            answer_id=answer_id,
            question=body.question,
            conversation=conversation,
            workspace_root=workspace_root,
        ),
        console_session_id=id,
    )
    return session


@router.post("/sessions/{id}/ask/cancel", response_model=ConsoleSession)
async def cancel_console_ask(id: str) -> ConsoleSession:
    """Stop generating. Distinct from ``POST /cancel``, which ends the whole session —
    abandoning a question is not a reason to throw away the conversation it was asked in."""
    await require_session(id)
    async with _lock_for(id):
        session = await require_session(id)
        await sync_ask_state(session)
        if not session.ask_in_flight or session.active_ask_id is None:
            raise HTTPException(status_code=409, detail="no answer is being generated")
        run_registry().cancel(session.active_ask_id, reason="cancelled by user")
        return session


async def _answer(
    *,
    session_id: str,
    ask_id: str,
    answer_id: str,
    question: str,
    conversation: str,
    workspace_root: Path,
) -> None:
    """The ask body, run by the RunRegistry once it holds the lane permit."""
    emitter = Emitter(event_log(), session_id, None, phase="ask")
    token = set_emitter(emitter)
    try:
        emit_live(
            agent_event(
                "explainer",
                "ask_started",
                "Answering",
                message_id=answer_id,
                ask_id=ask_id,
                question=question[:500],
            )
        )
        # Explicit rather than left to the first model call: a cold lane is minutes, and
        # ensure_lane emits lane_loading/lane_ready so the UI can say so (M4).
        await ensure_lane(Role.WORK)

        def on_delta(text: str) -> None:
            emit_live(
                agent_event(
                    "explainer",
                    "answer_delta",
                    # The chunk lives in ``detail``; the summary stays a fixed shape so a
                    # generic timeline renderer shows a progress line rather than pasting
                    # the answer into itself one fragment at a time.
                    f"+{len(text)} chars",
                    level="debug",
                    message_id=answer_id,
                    text=text,
                )
            )

        answer = await run_explainer(
            question=question,
            workspace_root=workspace_root,
            session_id=session_id,
            conversation=conversation,
            on_delta=on_delta,
        )
        for citation in answer.citations:
            emit_live(
                agent_event(
                    "explainer",
                    "citation",
                    f"{citation.path}" + (f":{citation.start_line}" if citation.start_line else ""),
                    level="debug",
                    message_id=answer_id,
                    **citation.model_dump(),
                )
            )
        await _record_answer(session_id, answer_id, answer.text, answer.citations)
        emit_live(
            agent_event(
                "explainer",
                "answer_complete",
                # The authoritative text: a client replaces the streamed bubble with this,
                # which is also what makes a replayed reconnect idempotent.
                answer.text[:2000],
                message_id=answer_id,
                text=answer.text,
                citations=[c.model_dump() for c in answer.citations],
                tool_calls=answer.tool_calls,
                truncated=answer.truncated,
            )
        )
    except (RunCancelled, asyncio.CancelledError):
        _fail(answer_id, "Cancelled before the answer was finished")
        raise
    except Exception as exc:
        logger.exception("ask %s failed for session %s", ask_id, session_id)
        _fail(answer_id, str(exc)[:500])
    finally:
        reset_emitter(token)
        # Best effort under a hard cancel, which can interrupt this await. Harmless:
        # ``ask_in_flight`` is derived from the registry on read, and the entry is gone.
        await _clear_ask(session_id, ask_id)


def _fail(answer_id: str, reason: str) -> None:
    emit_live(
        agent_event(
            "explainer",
            "ask_failed",
            "Could not answer",
            level="error",
            message_id=answer_id,
            error=reason,
        )
    )


async def _record_answer(
    session_id: str, answer_id: str, text: str, citations: list[AnswerCitation]
) -> None:
    """Append the answer under the per-session lock, re-reading first.

    Never mutates the session object the request handler held: a message posted while the
    model was thinking would be silently dropped by saving a stale copy.
    """
    async with _lock_for(session_id):
        session = await load_session(session_id)
        if session is None:
            return  # deleted or reaped mid-answer
        session.messages.append(
            ConsoleMessage(
                role=ConsoleMessageRole.ASSISTANT,
                content=text,
                message_id=answer_id,
                citations=list(citations),
            )
        )
        await touch(session)


async def _clear_ask(session_id: str, ask_id: str) -> None:
    async with _lock_for(session_id):
        session = await load_session(session_id)
        if session is None or session.active_ask_id != ask_id:
            return
        session.active_ask_id = None
        session.ask_in_flight = False
        await touch(session)


@router.get("/sessions/{id}/files/{path:path}", response_model=ConsoleFile)
async def get_console_file(id: str, path: str) -> ConsoleFile:
    """One file from the session's checkout, so a citation is a link and not just text.

    Read-only and path-guarded by ``resolve_safe_path``, the same gate every agent tool goes
    through — this endpoint must not become a way to read files a tool could not.
    """
    await require_session(id)
    session = await require_session(id)
    if not session.workspace_root or session.workspace_status is not WorkspaceStatus.READY:
        raise HTTPException(
            status_code=409,
            detail=f"repository is {session.workspace_status.value}; no files to read",
        )
    root = Path(session.workspace_root)
    try:
        target = await asyncio.to_thread(resolve_safe_path, path, root=root.resolve())
    except UnsafePathError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return await asyncio.to_thread(_read_capped, target, path)


def _read_capped(target: Path, path: str) -> ConsoleFile:
    if not target.is_file():
        raise HTTPException(status_code=404, detail=f"{path} is not a file in this checkout")
    size = target.stat().st_size
    cap = get_settings().console_file_max_bytes
    try:
        raw = target.read_bytes()[:cap]
    except OSError as exc:
        raise HTTPException(status_code=404, detail=f"cannot read {path}: {exc}") from exc
    return ConsoleFile(
        path=path.lstrip("./"),
        content=raw.decode("utf-8", errors="replace"),
        size_bytes=size,
        truncated=size > cap,
    )
