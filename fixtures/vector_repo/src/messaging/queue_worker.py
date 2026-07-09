"""Background worker for ferry queue processing — implemented in story 1."""

from __future__ import annotations

from messaging.ferry import MessageFerry
from storage.sqlite_repo import SqliteRepository


class QueueWorker:
    """Drains outbound queue through the message ferry."""

    def __init__(self, repo: SqliteRepository, ferry: MessageFerry | None = None) -> None:
        self._repo = repo
        self._ferry = ferry or MessageFerry()

    def process_once(self) -> int:
        """Process all pending messages. Story 1 must wire repo dequeue to ferry dispatch."""
        raise NotImplementedError("story 1: wire repo dequeue to ferry")
