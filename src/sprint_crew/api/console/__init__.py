"""Live /v1/console/* routes. Layout and invariants: AGENTS.md §4.2."""

from __future__ import annotations

# Imported for their route-registration side effect; the router is fully populated here.
from sprint_crew.api.console import ask as _ask  # noqa: F401
from sprint_crew.api.console import diffs as _diffs  # noqa: F401
from sprint_crew.api.console import events as _events  # noqa: F401
from sprint_crew.api.console import plan as _plan  # noqa: F401
from sprint_crew.api.console import routes as _routes  # noqa: F401
from sprint_crew.api.console.state import (
    lock_count,
    reset_console_locks,
    reset_console_store,
    router,
    run_console_reaper,
)

__all__ = [
    "lock_count",
    "reset_console_locks",
    "reset_console_store",
    "router",
    "run_console_reaper",
]
