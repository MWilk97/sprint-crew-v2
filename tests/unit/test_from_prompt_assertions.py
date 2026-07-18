from __future__ import annotations

import pytest
from tests.helpers.from_prompt_assertions import classify_integration_failure

from sprint_crew.schemas.session import BacklogRun, BacklogRunStatus


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
