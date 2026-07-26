from __future__ import annotations

from unittest.mock import MagicMock, patch

from sprint_crew.config import Role
from sprint_crew.inference.structured import structured_completion
from sprint_crew.schemas.change import CodeChange


def test_structured_completion_retries_on_invalid_json() -> None:
    bad = MagicMock()
    bad.choices = [MagicMock(message=MagicMock(content='{"ticket_key":'))]

    good_change = CodeChange(
        ticket_key="DEMO-1",
        branch="feature/demo-1",
        summary="ok",
        tests_passed=True,
    )
    good = MagicMock()
    good.choices = [MagicMock(message=MagicMock(content=good_change.model_dump_json()))]

    client = MagicMock()
    client.chat.completions.create.side_effect = [bad, good]

    with patch("sprint_crew.inference.structured._client", return_value=client):
        result = structured_completion(
            Role.WORK,
            system_prompt="sys",
            user_prompt="user",
            output_type=CodeChange,
            max_retries=2,
        )

    assert result.ticket_key == "DEMO-1"
    assert client.chat.completions.create.call_count == 2


def test_structured_completion_strips_markdown_fences() -> None:
    change = CodeChange(
        ticket_key="DEMO-1",
        branch="feature/demo-1",
        summary="ok",
        tests_passed=True,
    )
    fenced = f"```json\n{change.model_dump_json()}\n```"
    resp = MagicMock()
    resp.choices = [MagicMock(message=MagicMock(content=fenced))]

    client = MagicMock()
    client.chat.completions.create.return_value = resp

    with patch("sprint_crew.inference.structured._client", return_value=client):
        result = structured_completion(
            Role.WORK,
            system_prompt="sys",
            user_prompt="user",
            output_type=CodeChange,
        )

    assert result.summary == "ok"
