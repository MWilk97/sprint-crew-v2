from __future__ import annotations

import subprocess
from pathlib import Path

from sprint_crew.orchestrator.merge_gate import review_accepted
from sprint_crew.orchestrator.pytest_cmd import resolve_pytest_bin
from sprint_crew.schemas.session import SessionStatus, SprintSession


def pytest_passes(workspace: Path, *, test_target: str | None = None) -> bool:
    pytest_bin = resolve_pytest_bin(workspace)
    argv = [pytest_bin, "-q"]
    if test_target:
        argv.append(test_target)
    proc = subprocess.run(
        argv,
        cwd=workspace,
        capture_output=True,
        text=True,
        check=False,
    )
    return proc.returncode == 0


def coverage_satisfied_from_session(session: SprintSession) -> bool:
    for event in reversed(session.events):
        if event.agent == "merge_gate" and event.event_type == "gate_result":
            detail = event.detail or {}
            if "coverage_satisfied" in detail:
                return bool(detail["coverage_satisfied"])
    return True


def count_semantic_search_events(session: SprintSession) -> int:
    return sum(
        1
        for event in session.events
        if event.event_type == "tool_call" and (event.detail or {}).get("tool") == "semantic_search"
    )


def count_semantic_retrieval_events(session: SprintSession) -> int:
    total = count_semantic_search_events(session)
    for event in session.events:
        if event.event_type != "pre_search":
            continue
        detail = event.detail or {}
        hits = detail.get("hits")
        if isinstance(hits, int) and hits > 0:
            total += 1
        elif isinstance(hits, list) and hits:
            total += 1
    return total


def assert_cycle_passed(
    session: SprintSession,
    *,
    workspace: Path | None = None,
    test_target: str | None = None,
) -> None:
    if session.review_outcome is None:
        raise AssertionError("Session has no review_outcome")
    review = session.review_outcome
    coverage_ok = coverage_satisfied_from_session(session)
    if not review_accepted(review, coverage_satisfied=coverage_ok):
        raise AssertionError(
            f"merge gate rejected review: passed={review.passed} "
            f"tests_passed={review.tests_passed} coverage_satisfied={coverage_ok}"
        )
    if session.status != SessionStatus.AWAITING_HUMAN:
        raise AssertionError(
            f"expected awaiting_human, got {session.status.value}; error={session.error}"
        )
    ws = workspace or Path(session.workspace_root)
    if not pytest_passes(ws, test_target=test_target):
        raise AssertionError("pytest not green in workspace")


def count_tool_call_events(session: SprintSession) -> int:
    return sum(1 for event in session.events if event.event_type == "tool_call")


def index_chunks_from_session(session: SprintSession) -> int | None:
    for event in session.events:
        if event.event_type == "vector_indexed":
            detail = event.detail or {}
            chunks = detail.get("chunks")
            if isinstance(chunks, int):
                return chunks
    return None


def semantic_retrieval_diagnostics(session: SprintSession) -> str:
    pre_search = sum(1 for event in session.events if event.event_type == "pre_search")
    tool_calls = count_semantic_search_events(session)
    skipped = sum(1 for event in session.events if event.event_type == "semantic_search_skipped")
    return (
        f"pre_search_events={pre_search}, semantic_search_tools={tool_calls}, "
        f"semantic_search_skipped={skipped}"
    )
