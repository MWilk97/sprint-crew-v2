from __future__ import annotations

import pytest

from sprint_crew.config import Role, lane_for_role


def test_lane_for_role_coding() -> None:
    lane = lane_for_role(Role.CODING)
    assert lane.served_name == "qwen3-coder-30b"
    assert "8001" in lane.base_url


@pytest.mark.asyncio
async def test_inference_router_import() -> None:
    from sprint_crew.inference.router import pydantic_ai_model, served_model_name

    assert served_model_name(Role.PLANNING) == "qwen3-14b"
    model = pydantic_ai_model(Role.CODING)
    assert model.model_name == "qwen3-coder-30b"
    assert "8001" in model.base_url
