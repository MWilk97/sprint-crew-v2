from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from tests.helpers import from_prompt_assertions as fpa
from tests.helpers.from_prompt_assertions import (
    classify_integration_failure,
    evaluate_from_prompt_run,
)
from tests.helpers.from_prompt_live import (
    POSTCHECK_QUERIES,
    PostCheckResult,
    postcheck_collection_id,
    verify_prompt_surfaces_path,
)

from sprint_crew.schemas.session import BacklogRun, BacklogRunStatus
from sprint_crew.vector.search import SearchHit


def test_postcheck_collection_id_prefixes_run_id() -> None:
    assert postcheck_collection_id("abc-123") == "postcheck-abc-123"
    assert postcheck_collection_id("postcheck-abc") == "postcheck-abc"


def test_verify_prompt_surfaces_path_reindexes_and_searches(tmp_path: Path) -> None:
    workspace = tmp_path / "repo"
    workspace.mkdir()
    ferry_hit = SearchHit(
        path="src/messaging/ferry.py",
        start_line=1,
        end_line=3,
        score=0.91,
        chunk_kind="code",
        snippet="ferry dispatch",
    )

    class FakeIndexResult:
        chunks = 42

    with (
        patch(
            "tests.helpers.from_prompt_live.maybe_index_shared", return_value=FakeIndexResult()
        ) as index_mock,
        patch(
            "tests.helpers.from_prompt_live.semantic_search",
            return_value=[ferry_hit],
        ) as search_mock,
    ):
        result = verify_prompt_surfaces_path(workspace, "run-abc", fragments=("ferry",))

    index_mock.assert_called_once()
    scope = index_mock.call_args[0][1]
    # Its own collection: the postcheck audits the run's index, it must not write into it.
    assert scope.shared == "code_chunks_run_postcheck-run-abc"
    search_mock.assert_called_once_with(
        (scope.shared,),
        POSTCHECK_QUERIES["ferry"],
        top_k=5,
    )
    assert isinstance(result, PostCheckResult)
    assert result.fragments_found == {"ferry": True}
    assert result.chunks == 42


def test_verify_prompt_surfaces_path_multi_fragment(tmp_path: Path) -> None:
    workspace = tmp_path / "repo"
    workspace.mkdir()

    def fake_search(collection_id: str, query: str, top_k: int = 5) -> list[SearchHit]:
        if query == POSTCHECK_QUERIES["ferry"]:
            return [
                SearchHit(
                    path="src/messaging/ferry.py",
                    start_line=1,
                    end_line=1,
                    score=0.9,
                    chunk_kind="code",
                    snippet="ferry",
                )
            ]
        return [
            SearchHit(
                path="src/messaging/retry_policy.py",
                start_line=1,
                end_line=1,
                score=0.88,
                chunk_kind="code",
                snippet="retry",
            )
        ]

    with (
        patch("tests.helpers.from_prompt_live.maybe_index_shared", return_value=None),
        patch("tests.helpers.from_prompt_live.semantic_search", side_effect=fake_search),
    ):
        result = verify_prompt_surfaces_path(workspace, "run-xyz", fragments=("ferry", "retry"))

    assert result.fragments_found == {"ferry": True, "retry": True}
    assert "ferry" in result.hits_by_fragment
    assert "retry" in result.hits_by_fragment


def test_verify_prompt_surfaces_path_raises_when_missing_fragment(tmp_path: Path) -> None:
    workspace = tmp_path / "repo"
    workspace.mkdir()
    with (
        patch("tests.helpers.from_prompt_live.maybe_index_shared", return_value=None),
        patch("tests.helpers.from_prompt_live.semantic_search", return_value=[]),
    ):
        with pytest.raises(AssertionError, match="ferry"):
            verify_prompt_surfaces_path(workspace, "run-fail", fragments=("ferry",))


@pytest.mark.parametrize(
    ("failure_msg", "run_error", "backlog_status", "expected"),
    [
        (
            "semantic index should surface ['ferry'], hits=[]",
            None,
            BacklogRunStatus.COMPLETED,
            "post_check",
        ),
        (
            "status=failed; error='Request timed out.'",
            "Request timed out.",
            BacklogRunStatus.FAILED,
            "infra_timeout",
        ),
        (
            "SCRUM-518: merge gate rejected review",
            "Scope violation: changes to out-of-scope files",
            BacklogRunStatus.FAILED,
            "merge_gate_coverage",
        ),
        (
            "status=failed; error='Expecting value: line 1 column 1'",
            "Expecting value",
            BacklogRunStatus.FAILED,
            "reviewer_json",
        ),
        (
            None,
            None,
            BacklogRunStatus.COMPLETED,
            "none",
        ),
    ],
)
def test_classify_integration_failure(
    failure_msg: str | None,
    run_error: str | None,
    backlog_status: BacklogRunStatus,
    expected: str,
) -> None:
    run = BacklogRun(
        run_id="test-run",
        status=backlog_status,
        user_prompt="prompt",
        error=run_error,
    )
    assert classify_integration_failure(run, [], failure_msg) == expected


def _post_check_eval(tmp_path: Path, *, post_check_fatal: bool):
    """Run evaluate_from_prompt_run with a FAILING post-check, pipeline + cycles green."""
    run = BacklogRun(run_id="run-x", status=BacklogRunStatus.COMPLETED, user_prompt="p")
    session = SimpleNamespace(
        session_id="s1",
        ticket_key="SCRUM-1",
        workspace_root=str(tmp_path),
        task_plan=None,
    )
    result = SimpleNamespace(
        run=run,
        plan=SimpleNamespace(stories=[object()]),
        sessions=[session],
        scrum_workspace_id="ws",
    )
    with (
        patch.object(fpa, "assert_pipeline_completed", return_value=None),
        patch.object(
            fpa, "run_post_check", side_effect=AssertionError("semantic index should surface")
        ),
        patch.object(fpa, "assert_per_session_cycles", return_value=None),
        patch.object(fpa, "assert_per_session_semantic", return_value=None),
        patch.object(fpa, "_session_metrics", return_value={"session_id": "s1"}),
        patch.object(fpa, "count_semantic_retrieval_events", return_value=0),
    ):
        return evaluate_from_prompt_run(
            result,  # type: ignore[arg-type]
            1.0,
            tier="trap",
            trap="from_prompt_3story",
            story_count_exact=None,
            story_count_range=(2, 3),
            post_check_fragments=("ferry",),
            assert_plan_fragments=False,
            post_check_fatal=post_check_fatal,
        )


def test_post_check_non_fatal_does_not_fail_trap(tmp_path: Path) -> None:
    check = _post_check_eval(tmp_path, post_check_fatal=False)
    # Brittle retrieval post-check failed, but the trap outcome is green:
    assert check.post_check_ok is False
    assert check.failure is None
    assert check.failure_class is None
    # Failure detail is still captured in the report, not silently dropped.
    assert check.post_check == {"error": "semantic index should surface"}


def test_post_check_fatal_fails_integration(tmp_path: Path) -> None:
    check = _post_check_eval(tmp_path, post_check_fatal=True)
    assert check.post_check_ok is False
    assert check.failure == "semantic index should surface"
    assert check.failure_class == "post_check"
