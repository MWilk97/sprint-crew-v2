from __future__ import annotations

import re
from pathlib import Path

import pytest
from pydantic import AliasChoices

from sprint_crew.config import Role, Settings, lane_for_role


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
    assert lane.served_name == "qwen3.6-35b-a3b-nvfp4"
    assert "8002" in lane.base_url
    assert lane.max_model_len == 131072
    assert lane.gpu_memory_utilization == 0.65
    # The Interpreter sends images on this lane; downstream roles stay text-only (ADR 0013).
    assert lane.supports_vision is True


def test_vllm_work_url_accepts_legacy_planner_env(monkeypatch: pytest.MonkeyPatch) -> None:
    from sprint_crew.config import Settings

    monkeypatch.delenv("VLLM_WORK_URL", raising=False)
    monkeypatch.setenv("VLLM_PLANNER_URL", "http://127.0.0.1:8999/v1")
    settings = Settings()
    assert settings.vllm_work_url == "http://127.0.0.1:8999/v1"


def test_every_setting_is_documented_in_env_example() -> None:
    """.env.example is the only operator-facing list of knobs; a setting absent from it is
    invisible. Five (CANCEL_GRACE_S, CONSOLE_SESSION_TTL_DAYS, MAX_BACKLOG_STORIES,
    MAX_CODER_TURNS, MAX_TESTER_TURNS) had drifted out before this test existed.
    """
    env_text = Path(__file__).resolve().parents[2].joinpath(".env.example").read_text()
    documented = set(re.findall(r"^#?\s*([A-Z][A-Z0-9_]*)=", env_text, flags=re.M))

    undocumented = set()
    for name, field in Settings.model_fields.items():
        alias = field.validation_alias
        if isinstance(alias, AliasChoices):
            names = {str(c).upper() for c in alias.choices}
        else:
            names = {str(alias or name).upper()}
        if not names & documented:
            undocumented.add(sorted(names)[0])

    assert not undocumented, f"settings missing from .env.example: {sorted(undocumented)}"


@pytest.mark.asyncio
async def test_inference_router_import() -> None:
    from sprint_crew.inference.router import pydantic_ai_model

    model = pydantic_ai_model(Role.CODING)
    assert model.model_name == "laguna-s-2.1-nvfp4"
    assert "8001" in model.base_url
