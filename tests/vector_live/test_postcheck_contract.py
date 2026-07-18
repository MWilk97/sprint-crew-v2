from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest

from tests.helpers.from_prompt_live import POSTCHECK_QUERIES, verify_prompt_surfaces_path


@pytest.mark.vector_live
def test_postcheck_contract_on_vector_repo_fixture(
    skip_unless_vector_live,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fast preflight (~15s): post-check helper surfaces ferry on vector_repo without vLLM."""
    monkeypatch.setenv("VECTOR_INDEX_ENABLED", "1")
    from sprint_crew.config import get_settings

    get_settings.cache_clear()

    root = Path(__file__).resolve().parents[2] / "fixtures" / "vector_repo"
    run_id = f"contract-{uuid4().hex[:8]}"
    result = verify_prompt_surfaces_path(root, run_id, fragments=("ferry",))
    assert result.fragments_found.get("ferry") is True
    assert result.collection_id.startswith("postcheck-")
    assert result.hits_by_fragment["ferry"]
    assert POSTCHECK_QUERIES["ferry"] in (
        "ferry dispatch outbound queue worker",
    )
