"""Run lifecycle: real task handles, a single-slot admission queue, real cancel (M5).

Runs were fire-and-forget ``BackgroundTasks.add_task`` calls: no handle, so no cancel,
no queue, and a row left ``running`` in SQLite lied forever after a restart. This module
owns the handles.

**One run at a time.** The GPU physically serialises everything — ``ensure_lane`` stops
every other lane before starting one (``graph/lanes.py``) — so a second concurrent run
would only thrash lane swaps. Admission is a single-permit semaphore and the wait is
surfaced as a ``queued`` status with a position, rather than pretending concurrency works.

**Wrapper/body split.** ``submit`` creates a *wrapper* task that parks on the semaphore and
then awaits a separate *body* task. ``cancel`` cancels the body, never the wrapper. That is
what makes the wrapper's ``finally`` able to ``await`` lane teardown: a cancelled task cannot
reliably await anything, and a lane left loaded after a botched cancel wedges a 23 GB model
on the GPU for everything else. The wrapper is never cancelled, so its teardown always runs
to completion.

**Two cancel paths, both must land on ``cancelled``.** ``CancelToken`` is checked
cooperatively at the checkpoints listed in ``docs/roadmap-backend-interactive-console.md``
M5 and raises ``RunCancelled`` — an ordinary ``Exception``, so the broad ``except Exception``
handlers in ``session.py`` / ``batch_cycle.py`` need an explicit arm ahead of them or a
cancel is misreported as a failure. If the body ignores the token (blocked in a subprocess),
a watchdog escalates to ``body_task.cancel()`` after ``CANCEL_GRACE_S``; that raises
``CancelledError``, which inherits from ``BaseException`` and so bypasses those same
handlers. Both paths are pinned by tests.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from collections.abc import Awaitable, Callable
from contextvars import ContextVar, Token
from dataclasses import dataclass, field
from typing import Literal

from sprint_crew.config import get_settings

logger = logging.getLogger(__name__)

RunState = Literal["queued", "running", "cancelling", "done"]


class RunCancelled(Exception):
    """Cooperative cancel: a checkpoint saw a requested token and unwound the run.

    Deliberately an ``Exception`` and not ``BaseException`` — it must be catchable by the
    orchestrator layers that own run/session status, which is why they each carry an
    explicit arm for it before their generic error handling.
    """


@dataclass
class CancelToken:
    """Per-run cancel flag, read at the checkpoints and by the graph node wrapper."""

    requested_at: float | None = None
    reason: str | None = None

    @property
    def cancelled(self) -> bool:
        return self.requested_at is not None

    def request(self, reason: str | None = None) -> None:
        """Idempotent: the first request wins, so a repeated Stop cannot reset the clock."""
        if self.requested_at is None:
            self.requested_at = time.monotonic()
            self.reason = reason

    def check(self) -> None:
        if self.requested_at is not None:
            raise RunCancelled(self.reason or "run cancelled")


_current_token: ContextVar[CancelToken | None] = ContextVar("_current_cancel_token", default=None)


def current_cancel_token() -> CancelToken | None:
    return _current_token.get()


def set_cancel_token(token: CancelToken | None) -> Token[CancelToken | None]:
    return _current_token.set(token)


def reset_cancel_token(token: Token[CancelToken | None]) -> None:
    _current_token.reset(token)


def cancel_requested() -> bool:
    """True when the enclosing run has a cancel pending. Cheap enough for a hot loop."""
    token = _current_token.get()
    return token is not None and token.cancelled


def check_cancelled() -> None:
    """Raise ``RunCancelled`` if the enclosing run was cancelled; no-op outside a run."""
    token = _current_token.get()
    if token is not None:
        token.check()


@dataclass
class RunEntry:
    run_id: str
    console_session_id: str | None
    token: CancelToken
    state: RunState = "queued"
    # The wrapper owns admission and teardown and is never cancelled; the body is the
    # cancel target. See the module docstring for why this split exists.
    wrapper_task: asyncio.Task[None] | None = None
    body_task: asyncio.Task[None] | None = None
    # Held so the hard-cancel watchdog is not garbage-collected mid-sleep, and so cancel()
    # can tell an escalation is already armed.
    escalate_task: asyncio.Task[None] | None = None
    created_at: float = field(default_factory=time.monotonic)


RunBody = Callable[[], Awaitable[object]]
Teardown = Callable[[], Awaitable[None]]
OnAdmit = Callable[[], Awaitable[None]]


class RunRegistry:
    """Process-wide handle table plus a one-permit admission queue."""

    def __init__(self) -> None:
        self._entries: dict[str, RunEntry] = {}
        self._waiting: deque[str] = deque()
        self._slot = asyncio.Semaphore(1)

    def submit(
        self,
        run_id: str,
        body: RunBody,
        *,
        console_session_id: str | None = None,
        on_admit: OnAdmit | None = None,
        teardown: Teardown | None = None,
    ) -> RunEntry:
        """Queue a run and return its entry immediately. Never blocks on admission.

        ``on_admit`` runs once the slot is won (persist ``running``, emit ``run_started``);
        ``teardown`` runs on every exit path including cancel, from the uncancelled wrapper.
        """
        entry = RunEntry(
            run_id=run_id,
            console_session_id=console_session_id,
            token=CancelToken(),
        )
        self._entries[run_id] = entry
        self._waiting.append(run_id)
        entry.wrapper_task = asyncio.create_task(
            self._run(entry, body, on_admit, teardown),
            name=f"run:{run_id}",
        )
        return entry

    async def _run(
        self,
        entry: RunEntry,
        body: RunBody,
        on_admit: OnAdmit | None,
        teardown: Teardown | None,
    ) -> None:
        try:
            async with self._slot:
                self._dequeue(entry.run_id)
                # A cancel that landed while parked on the semaphore must not start work.
                if entry.token.cancelled:
                    entry.state = "done"
                    return
                entry.state = "running"
                if on_admit is not None:
                    await on_admit()
                token = set_cancel_token(entry.token)
                try:
                    entry.body_task = asyncio.create_task(
                        self._body(body), name=f"run-body:{entry.run_id}"
                    )
                    await entry.body_task
                except asyncio.CancelledError:
                    # Hard escalation reached the body. The wrapper itself is not cancelled,
                    # so swallowing here is correct and lets teardown below run normally.
                    logger.info("run %s body cancelled", entry.run_id)
                except RunCancelled:
                    logger.info("run %s cancelled cooperatively", entry.run_id)
                except Exception:
                    # The body owns persisting its own failure (BacklogRun/SprintSession
                    # status). Swallow here so the wrapper task never carries an
                    # unretrieved exception, and so teardown below still runs.
                    logger.exception("run %s failed", entry.run_id)
                finally:
                    reset_cancel_token(token)
        except asyncio.CancelledError:
            # Only reachable while parked on the semaphore (a queued run cancelled before
            # it ever started). Nothing to unwind; re-raise to honour task semantics.
            self._dequeue(entry.run_id)
            entry.state = "done"
            raise
        finally:
            entry.state = "done"
            self._entries.pop(entry.run_id, None)
            if teardown is not None:
                try:
                    await teardown()
                except Exception:
                    logger.exception("run %s teardown failed", entry.run_id)

    async def _body(self, body: RunBody) -> None:
        # The token contextvar is captured when this task is created, so everything below
        # (including asyncio.to_thread workers) sees the same token.
        await body()

    def _dequeue(self, run_id: str) -> None:
        try:
            self._waiting.remove(run_id)
        except ValueError:
            pass

    def cancel(self, run_id: str, *, reason: str | None = None) -> bool:
        """Request cancel. Returns False for an unknown/finished run.

        A queued run stops instantly — it is parked on the semaphore with nothing to
        unwind. A running run gets the cooperative token plus a watchdog that escalates
        to a hard cancel after ``CANCEL_GRACE_S``, because a body blocked inside
        ``subprocess.run`` cannot see the token until it returns.
        """
        entry = self._entries.get(run_id)
        if entry is None or entry.state == "done":
            return False
        entry.token.request(reason)
        if entry.state == "queued":
            self._dequeue(run_id)
            entry.state = "done"
            if entry.wrapper_task is not None:
                entry.wrapper_task.cancel()
            return True
        if entry.state == "cancelling":
            # Already escalating. Re-arming would spawn a second untracked watchdog per
            # repeated Stop (double-click, client retry) — harmless individually, but the
            # tasks are unreferenced and nothing bounds how many a user can create.
            return True
        entry.state = "cancelling"
        entry.escalate_task = asyncio.create_task(
            self._escalate(entry), name=f"run-cancel:{run_id}"
        )
        return True

    async def _escalate(self, entry: RunEntry) -> None:
        await asyncio.sleep(get_settings().cancel_grace_s)
        body = entry.body_task
        if body is not None and not body.done():
            logger.warning(
                "run %s ignored cooperative cancel for %.0fs; hard-cancelling",
                entry.run_id,
                get_settings().cancel_grace_s,
            )
            body.cancel()

    def get(self, run_id: str) -> RunEntry | None:
        return self._entries.get(run_id)

    def position(self, run_id: str) -> int | None:
        """How many runs must finish before this one starts. None once admitted or unknown.

        0 means "starting now" — nothing is ahead and the slot is free. A freshly submitted
        lone run sits in ``_waiting`` until its task first runs, so counting raw queue index
        would report it as waiting on nothing and make a single run flicker queued→running.
        """
        try:
            index = self._waiting.index(run_id)
        except ValueError:
            return None
        return index + (1 if self.active_run_id() is not None else 0)

    def active_run_id(self) -> str | None:
        return next(
            (rid for rid, e in self._entries.items() if e.state in ("running", "cancelling")),
            None,
        )

    def queue_depth(self) -> int:
        return len(self._waiting)

    def reset(self) -> None:
        """Test hook: cancel everything outstanding and drop the table."""
        for entry in list(self._entries.values()):
            for task in (entry.body_task, entry.wrapper_task):
                if task is not None and not task.done():
                    task.cancel()
        self._entries.clear()
        self._waiting.clear()
        self._slot = asyncio.Semaphore(1)


_registry: RunRegistry | None = None


def run_registry() -> RunRegistry:
    global _registry
    if _registry is None:
        _registry = RunRegistry()
    return _registry


def reset_run_registry() -> None:
    global _registry
    if _registry is not None:
        _registry.reset()
    _registry = None
