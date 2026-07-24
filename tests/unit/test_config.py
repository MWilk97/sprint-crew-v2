from __future__ import annotations

import pytest

from sprint_crew.config import Role, lane_for_role


def test_lane_for_role_coding() -> None:
    lane = lane_for_role(Role.CODING)
    assert lane.served_name == "laguna-s-2.1-nvfp4"
    assert "8001" in lane.base_url
    assert lane.is_moe is True
    assert lane.request_limit_multiplier == 1.25
    assert lane.max_model_len == 131072
    assert lane.gpu_memory_utilization == 0.85


def test_lane_for_role_work() -> None:
    lane = lane_for_role(Role.WORK)
    assert lane.served_name == "qwen3-30b-a3b-thinking"
    assert "8002" in lane.base_url
    assert lane.max_model_len == 131072
    assert lane.gpu_memory_utilization == 0.50


def test_vllm_work_url_accepts_legacy_planner_env(monkeypatch: pytest.MonkeyPatch) -> None:
    from sprint_crew.config import Settings

    monkeypatch.delenv("VLLM_WORK_URL", raising=False)
    monkeypatch.setenv("VLLM_PLANNER_URL", "http://127.0.0.1:8999/v1")
    settings = Settings()
    assert settings.vllm_work_url == "http://127.0.0.1:8999/v1"


@pytest.mark.asyncio
async def test_inference_router_import() -> None:
    from sprint_crew.inference.router import pydantic_ai_model

    model = pydantic_ai_model(Role.CODING)
    assert model.model_name == "laguna-s-2.1-nvfp4"
    assert "8001" in model.base_url
