from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
from tests.helpers.from_prompt_assertions import classify_integration_failure
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
            "tests.helpers.from_prompt_live.maybe_index_workspace", return_value=FakeIndexResult()
        ) as index_mock,
        patch(
            "tests.helpers.from_prompt_live.semantic_search",
            return_value=[ferry_hit],
        ) as search_mock,
    ):
        result = verify_prompt_surfaces_path(workspace, "run-abc", fragments=("ferry",))

    index_mock.assert_called_once()
    assert index_mock.call_args[0][1] == "postcheck-run-abc"
    search_mock.assert_called_once_with(
        "postcheck-run-abc",
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
        patch("tests.helpers.from_prompt_live.maybe_index_workspace", return_value=None),
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
        patch("tests.helpers.from_prompt_live.maybe_index_workspace", return_value=None),
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
