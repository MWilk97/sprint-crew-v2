from __future__ import annotations

from enum import Enum
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Role(str, Enum):
    CODING = "coding"
    PLANNING = "planning"
    REVIEWING = "reviewing"


class LaneConfig(BaseModel):
    model_config = ConfigDict(extra="ignore")

    port: int
    base_url: str
    model_id: str
    served_name: str
    max_model_len: int = 8192
    gpu_memory_utilization: float = 0.28


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    hf_token: str = Field(default="", alias="HF_TOKEN")
    hf_home: Path = Field(default=Path.home() / "vllm-models", alias="HF_HOME")

    vllm_coder_url: str = Field(default="http://127.0.0.1:8001/v1", alias="VLLM_CODER_URL")
    vllm_planner_url: str = Field(default="http://127.0.0.1:8002/v1", alias="VLLM_PLANNER_URL")
    vllm_judge_url: str = Field(default="http://127.0.0.1:8003/v1", alias="VLLM_JUDGE_URL")

    max_review_retries: int = Field(default=4, alias="MAX_REVIEW_RETRIES")

    @property
    def project_root(self) -> Path:
        return Path(__file__).resolve().parents[2]


@lru_cache
def get_settings() -> Settings:
    return Settings()


@lru_cache
def load_models_yaml(path: Path | None = None) -> dict[str, Any]:
    root = get_settings().project_root
    yaml_path = path or root / "infra" / "models.yaml"
    with yaml_path.open(encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def lane_for_role(role: Role) -> LaneConfig:
    data = dict(load_models_yaml()["lanes"][role.value])
    settings = get_settings()
    data["base_url"] = {
        Role.CODING: settings.vllm_coder_url,
        Role.PLANNING: settings.vllm_planner_url,
        Role.REVIEWING: settings.vllm_judge_url,
    }[role]
    return LaneConfig(**data)
