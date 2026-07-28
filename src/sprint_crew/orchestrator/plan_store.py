"""The reviewed backlog, kept so Promote can execute it verbatim (roadmap M10).

``ConsolePlanResult`` is the *view* a client renders: titles, rationales, file lists. It is
lossy on purpose. Promote needs the ``BacklogPlan`` the ScrumMaster actually produced —
story keys, dependency edges, ``recommended_first`` — because that is what
``run_backlog_batched`` consumes. Without it, promoting would mean re-planning, and the
backlog that ran could differ from the one the user read and approved.

One row per console session: a session plans once, and re-planning replaces the row.
"""

from __future__ import annotations

from datetime import UTC, datetime

from pydantic import BaseModel, Field

from sprint_crew.config import get_settings
from sprint_crew.orchestrator.store import TypedJsonStore, _cached_store
from sprint_crew.schemas._base import STRICT
from sprint_crew.schemas.backlog import BacklogPlan


def _utc_now_iso() -> str:
    return datetime.now(tz=UTC).isoformat()


class StoredPlan(BaseModel):
    """A plan session's backlog plus the prompt it came from.

    Not a wire model — the client sees ``ConsolePlanResult``. The prompt travels with the
    plan because a promoted run needs the same ``user_prompt`` the plan was built against;
    re-deriving it from the session's messages would drift once M9 made messages editable
    across clarify rounds.
    """

    model_config = STRICT

    session_id: str = Field(..., min_length=1)
    plan: BacklogPlan
    prompt: str = Field(..., min_length=1)
    created_at: str = Field(default_factory=_utc_now_iso)


class PlanStore(TypedJsonStore[StoredPlan]):
    model = StoredPlan
    table = "console_plans"
    key_column = "session_id"
    create_sql = """
        CREATE TABLE IF NOT EXISTS console_plans (
            session_id TEXT PRIMARY KEY,
            payload TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
    """

    def _extra_columns(self, item: StoredPlan) -> dict[str, str]:
        return {"created_at": item.created_at}

    def delete(self, session_id: str) -> bool:
        return self._delete(session_id)

    def clear(self) -> None:
        self._clear_all()


def plan_store() -> PlanStore:
    return _cached_store(PlanStore, get_settings().session_db)
