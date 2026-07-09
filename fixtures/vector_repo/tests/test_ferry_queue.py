"""Story 1 — persistent outbound message queue via ferry dispatch layer."""

from __future__ import annotations

from pathlib import Path

import pytest

from messaging.ferry import MessageFerry
from messaging.models import DeliveryStatus, Message
from storage.sqlite_repo import SqliteRepository


@pytest.fixture
def repo(tmp_path: Path) -> SqliteRepository:
    return SqliteRepository(tmp_path / "queue.db")


def test_enqueue_persists_pending_message(repo: SqliteRepository) -> None:
    msg = Message(id="m1", recipient="a@b.com", body="hi")
    repo.enqueue_message(msg)
    pending = repo.dequeue_pending()
    assert len(pending) == 1
    assert pending[0].id == "m1"
    assert pending[0].status == DeliveryStatus.PENDING


def test_ferry_dispatches_dequeued_message(repo: SqliteRepository) -> None:
    msg = Message(id="m2", recipient="a@b.com", body="notify")
    repo.enqueue_message(msg)
    ferry = MessageFerry()
    for item in repo.dequeue_pending():
        status = ferry.send_now(item)
        repo.update_message_status(item.id, status)
    # verify status persisted
    pending = repo.dequeue_pending()
    assert pending == []


def test_queue_worker_drains_pending(repo: SqliteRepository) -> None:
    from messaging.queue_worker import QueueWorker

    msg = Message(id="m3", recipient="a@b.com", body="worker")
    repo.enqueue_message(msg)
    ferry = MessageFerry(repository=repo)
    worker = QueueWorker(repo, ferry)
    processed = worker.process_once()
    assert processed == 1
    assert repo.dequeue_pending() == []
