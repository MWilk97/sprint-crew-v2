from __future__ import annotations

from enum import Enum
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from pydantic import AliasChoices, BaseModel, ConfigDict, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Role(str, Enum):
    CODING = "coding"
    WORK = "work"


class LaneConfig(BaseModel):
    model_config = ConfigDict(extra="ignore")

    port: int
    base_url: str
    model_id: str
    served_name: str
    max_model_len: int = 8192
    gpu_memory_utilization: float = 0.28
    is_moe: bool = False
    request_limit_multiplier: float = 1.0


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    hf_token: str = Field(default="", alias="HF_TOKEN")
    hf_home: Path = Field(default=Path.home() / "vllm-models", alias="HF_HOME")

    vllm_coder_url: str = Field(default="http://127.0.0.1:8001/v1", alias="VLLM_CODER_URL")
    vllm_work_url: str = Field(
        default="http://127.0.0.1:8002/v1",
        validation_alias=AliasChoices("VLLM_WORK_URL", "VLLM_PLANNER_URL"),
    )

    max_review_retries: int = Field(default=4, alias="MAX_REVIEW_RETRIES")
    max_plan_retries: int = Field(default=1, alias="MAX_PLAN_RETRIES")
    max_backlog_stories: int = Field(default=5, alias="MAX_BACKLOG_STORIES")

    workspace_base: Path = Field(
        default=Path.home() / "sprint-workspaces",
        alias="SPRINT_WORKSPACE_BASE",
    )
    checkpoint_db: Path = Field(
        default=Path.home() / ".sprint-crew" / "checkpoints.db",
        alias="SPRINT_CHECKPOINT_DB",
    )
    session_db: Path = Field(
        default=Path.home() / ".sprint-crew" / "sessions.db",
        alias="SPRINT_SESSION_DB",
    )

    jira_url: str = Field(default="", alias="JIRA_URL")
    jira_email: str = Field(default="", alias="JIRA_EMAIL")
    jira_api_token: str = Field(default="", alias="JIRA_API_TOKEN")
    jira_project_key: str = Field(default="DEMO", alias="JIRA_PROJECT_KEY")
    jira_ac_field: str = Field(default="", alias="JIRA_AC_FIELD")
    jira_review_transition: str = Field(default="In Review", alias="JIRA_REVIEW_TRANSITION")
    github_token: str = Field(default="", alias="GITHUB_TOKEN")
    github_repo: str = Field(default="", alias="GITHUB_REPO")
    github_fixture_repo_greeter: str = Field(default="", alias="GITHUB_FIXTURE_REPO_GREETER")
    max_coder_turns: int = Field(default=32, alias="MAX_CODER_TURNS")
    max_tester_turns: int = Field(default=32, alias="MAX_TESTER_TURNS")
    coder_early_exit_requires_coverage: bool = Field(
        default=True, alias="CODER_EARLY_EXIT_REQUIRES_COVERAGE"
    )
    max_coverage_rounds: int = Field(default=2, alias="MAX_COVERAGE_ROUNDS")
    coder_step_mode: bool = Field(default=True, alias="CODER_STEP_MODE")
    max_techlead_turns: int = Field(default=12, alias="MAX_TECHLEAD_TURNS")
    max_write_file_bytes: int = Field(default=65536, alias="MAX_WRITE_FILE_BYTES")
    use_mock_integrations: bool = Field(default=True, alias="USE_MOCK_INTEGRATIONS")

    vector_index_enabled: bool = Field(default=True, alias="VECTOR_INDEX_ENABLED")
    qdrant_url: str = Field(default="http://127.0.0.1:6333", alias="QDRANT_URL")
    qdrant_collection_prefix: str = Field(default="code_chunks", alias="QDRANT_COLLECTION_PREFIX")
    embed_url: str = Field(default="http://127.0.0.1:8080/v1", alias="EMBED_URL")
    embed_model_id: str = Field(
        default="jinaai/jina-embeddings-v2-base-code",
        alias="EMBED_MODEL_ID",
    )
    vector_top_k: int = Field(default=8, alias="VECTOR_TOP_K")
    vector_score_threshold: float = Field(default=0.55, alias="VECTOR_SCORE_THRESHOLD")

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
        Role.WORK: settings.vllm_work_url,
    }[role]
    return LaneConfig(**data)
