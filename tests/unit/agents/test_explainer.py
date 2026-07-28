"""Explainer: citation derivation, delta coalescing, and the shared-only index scope (M9)."""

from __future__ import annotations

from pathlib import Path

import pytest

from sprint_crew.agents.explainer import DeltaBuffer, derive_citations
from sprint_crew.config import get_settings
from sprint_crew.tools.pydantic_ai import workspace_deps


def _read(path: str, *, start: int | None = None, end: int | None = None, ok: bool = True) -> dict:
    args: dict = {"path": path}
    if start is not None:
        args["start_line"] = start
    if end is not None:
        args["end_line"] = end
    return {"tool": "read_file", "args": args, "ok": ok, "output_preview": "..."}


def _search(preview: str) -> dict:
    return {
        "tool": "semantic_search",
        "args": {"query": "where is auth"},
        "ok": True,
        "output_preview": preview,
    }


# --- citations ----------------------------------------------------------------


def test_citations_follow_the_answer_and_carry_the_read_range() -> None:
    log = [_read("src/api/auth.py", start=10, end=40), _read("src/api/app.py")]
    answer = "Auth is enforced in src/api/auth.py, wired into src/api/app.py."

    citations = derive_citations(answer, log)

    assert [c.path for c in citations] == ["src/api/auth.py", "src/api/app.py"]
    assert (citations[0].start_line, citations[0].end_line) == (10, 40)
    assert citations[0].source == "read_file"


def test_inline_line_reference_beats_the_read_range() -> None:
    """`path:120` is the model pointing at a line; the read range is only where it looked."""
    log = [_read("src/api/auth.py", start=1, end=200)]

    citations = derive_citations("The check is at src/api/auth.py:120.", log)

    assert (citations[0].start_line, citations[0].end_line) == (120, None)


def test_hallucinated_paths_are_dropped(tmp_path: Path) -> None:
    """A path the agent never touched and that does not exist would be a broken link."""
    log = [_read("src/real.py")]

    citations = derive_citations(
        "Look at src/real.py, and also src/invented.py.", log, workspace_root=tmp_path
    )

    assert [c.path for c in citations] == ["src/real.py"]


def test_unopened_but_existing_path_is_cited_as_answer_sourced(tmp_path: Path) -> None:
    (tmp_path / "README.md").write_text("hi", encoding="utf-8")

    citations = derive_citations("See README.md.", [], workspace_root=tmp_path)

    assert [(c.path, c.source) for c in citations] == [("README.md", "answer")]


def test_answer_citing_nothing_falls_back_to_what_was_opened() -> None:
    """Still a truthful "here is what this was based on"."""
    log = [_read("src/a.py"), _read("src/b.py")]

    citations = derive_citations("It does not do that anywhere.", log)

    assert [c.path for c in citations] == ["src/a.py", "src/b.py"]
    assert {c.source for c in citations} == {"read_file"}


def test_semantic_search_hits_are_parsed_for_the_fallback() -> None:
    log = [_search("src/vector/search.py:12-40 (score=0.812, kind=code)\ndef semantic_search(")]

    citations = derive_citations("Nothing conclusive.", log)

    assert [(c.path, c.start_line, c.end_line) for c in citations] == [
        ("src/vector/search.py", 12, 40)
    ]
    assert citations[0].source == "semantic_search"


def test_failed_tool_calls_are_not_evidence() -> None:
    citations = derive_citations("Nothing found.", [_read("src/missing.py", ok=False)])

    assert citations == []


def test_citations_are_capped() -> None:
    log = [_read(f"src/f{i}.py") for i in range(30)]

    assert len(derive_citations("nothing", log)) == 12


def test_citations_deduplicate_repeated_paths() -> None:
    log = [_read("src/a.py", start=1, end=5), _read("src/a.py", start=50, end=60)]

    citations = derive_citations("src/a.py does it, see src/a.py again.", log)

    assert len(citations) == 1
    # The later read wins: it is usually the narrowing one.
    assert (citations[0].start_line, citations[0].end_line) == (50, 60)


# --- delta coalescing ---------------------------------------------------------


def test_buffer_flushes_on_size() -> None:
    flushed: list[str] = []
    buffer = DeltaBuffer(flushed.append, max_chars=5, interval_s=999, now=lambda: 0.0)

    buffer.add("abc")
    assert flushed == []
    buffer.add("de")
    assert flushed == ["abcde"]


def test_buffer_flushes_on_age_so_a_slow_answer_still_moves() -> None:
    flushed: list[str] = []
    # Constructed at t=0; the add() lands a full 10s later, past the 1s interval.
    clock = iter([0.0, 10.0, 10.0])
    buffer = DeltaBuffer(flushed.append, max_chars=999, interval_s=1.0, now=lambda: next(clock))

    buffer.add("a")

    assert flushed == ["a"]


def test_buffer_flush_is_idempotent_and_skips_empty() -> None:
    flushed: list[str] = []
    buffer = DeltaBuffer(flushed.append, max_chars=999, interval_s=999, now=lambda: 0.0)

    buffer.flush()
    buffer.add("x")
    buffer.flush()
    buffer.flush()

    assert flushed == ["x"]


# --- index scope --------------------------------------------------------------


@pytest.mark.parametrize("overlay", [True, False])
def test_index_overlay_switch_controls_the_searched_collections(
    tmp_path: Path, monkeypatch, overlay: bool
) -> None:
    """An ask reads a pristine checkout, so it must search the shared repo index only.

    With an overlay it would query a collection that does not exist, and would name it after
    the console session id — colliding with the overlay a later run on that session creates.
    """
    monkeypatch.setenv("VECTOR_INDEX_ENABLED", "true")
    get_settings.cache_clear()
    try:
        deps = workspace_deps(
            tmp_path,
            mutate=False,
            session_id="cs-abc123",
            include_semantic_search=True,
            index_overlay=overlay,
        )
        tool = deps.registry.get("semantic_search")
        assert tool is not None
        collections = tool._collections
        assert len(collections) == (2 if overlay else 1)
        assert any("cs-abc123" in c for c in collections) is overlay
    finally:
        get_settings.cache_clear()
