"""Live /v1/console/* routes (docs/contracts/chat-console-api.md, ADR 0011/0012).

Implementation notes:

- Sessions are durable in SQLite (``ConsoleSessionStore``) and survive a restart.
  Single-worker assumption still holds: the per-session asyncio locks in ``state`` are
  process-local, so this is not safe across multiple API workers.
- Clarify questions come from the Interpreter on the Work lane (ADR 0013). When the
  lane is cold or the call fails, the deterministic stub answers instead — an
  interactive caller must not block on a model load.
- mode=code start reuses the same orchestration as POST /sprint/from-prompt
  (``start_from_prompt_run`` in app.py); mode=plan never touches
  from-prompt/ship/Jira/git.
- Start queues the run and returns 202 (M5); planning happens in the registry-owned task
  and reports on the event stream. Cancel is real: it stops a queued run outright and asks
  a running one to unwind at its next checkpoint.

Layout: ``state`` (router, locks, persistence seam) ← ``clarify`` (Interpreter + answer
validation) and ``run_bridge`` (console↔backlog-run translation) ← ``routes`` (session
lifecycle) and ``events`` (polling + SSE transport). Importing this package registers
every route on ``router``.
"""

from __future__ import annotations

from sprint_crew.api.console import events as _events
from sprint_crew.api.console import routes as _routes
from sprint_crew.api.console.clarify import (
    apply_clarify_answers,
    build_clarify_questions,
    enter_clarifying,
)
from sprint_crew.api.console.run_bridge import (
    build_plan_result,
    build_run_prompt,
    sync_sprint_progress,
)
from sprint_crew.api.console.state import (
    _locks,
    reset_console_store,
    router,
    run_console_reaper,
)

# Route modules are imported for their registration side effect; name them so linters do
# not strip the imports and readers know the router is fully populated here.
get_console_events = _events.get_console_events
stream_console_events = _events.stream_console_events
create_console_session = _routes.create_console_session
get_console_session = _routes.get_console_session
post_console_message = _routes.post_console_message
submit_clarify_answers = _routes.submit_clarify_answers
confirm_console_session = _routes.confirm_console_session
start_console_run = _routes.start_console_run
cancel_console_session = _routes.cancel_console_session

__all__ = [
    "apply_clarify_answers",
    "build_clarify_questions",
    "build_plan_result",
    "build_run_prompt",
    "cancel_console_session",
    "confirm_console_session",
    "create_console_session",
    "enter_clarifying",
    "get_console_events",
    "get_console_session",
    "post_console_message",
    "reset_console_store",
    "router",
    "run_console_reaper",
    "start_console_run",
    "stream_console_events",
    "submit_clarify_answers",
    "sync_sprint_progress",
    "_locks",
]
