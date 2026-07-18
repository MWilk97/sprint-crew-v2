from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
from tests.helpers.from_prompt_live import (
    POSTCHECK_QUERIES,
    PostCheckResult,
    postcheck_collection_id,
    verify_prompt_surfaces_path,
)

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
        patch("tests.helpers.from_prompt_live.maybe_index_workspace", return_value=FakeIndexResult()) as index_mock,
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
