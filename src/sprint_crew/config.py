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
    supports_vision: bool = False
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
    max_plan_retries: int = Field(default=2, alias="MAX_PLAN_RETRIES")
    max_backlog_stories: int = Field(default=5, alias="MAX_BACKLOG_STORIES")

    # Shared bearer for the console + /sprint API (LAN single-user). Empty ⇒ auth disabled
    # so unit tests and smoke_cycle keep working without a header. GET /health is exempt.
    console_api_token: str = Field(default="", alias="CONSOLE_API_TOKEN")
    # Comma-separated browser origins for CORS (ADR 0011). Default "*" is DEV ONLY.
    console_cors_origins: str = Field(default="*", alias="CONSOLE_CORS_ORIGINS")

    # SSE event stream (roadmap M3). Heartbeat keeps idle connections alive through
    # proxy idle-reap and GPU-work silences. Queue maxsize bounds per-subscriber memory;
    # on overflow the subscriber is dropped and reconnects to replay from the events table.
    sse_heartbeat_s: float = Field(default=15.0, alias="SSE_HEARTBEAT_S")
    sse_queue_maxsize: int = Field(default=1000, alias="SSE_QUEUE_MAXSIZE")

    # Structured diff snapshots for the console review surface (roadmap M6). Captured once
    # per review pass, so the caps bound one attempt's payload, not a whole run's. A file
    # over the byte cap is shortened and flagged rather than dropped.
    diff_snapshot_enabled: bool = Field(default=True, alias="DIFF_SNAPSHOT_ENABLED")
    diff_max_files: int = Field(default=500, alias="DIFF_MAX_FILES")
    diff_max_file_bytes: int = Field(default=200_000, alias="DIFF_MAX_FILE_BYTES")

    # Human review gate before ship (roadmap M7). A parked run holds the single admission
    # slot and its workspace, so the timeout is what stops an abandoned review from
    # wedging the queue — see ADR 0015. Rejection rounds are a budget separate from
    # MAX_REVIEW_RETRIES: a picky human must not exhaust the machine's own retries.
    console_review_gate_enabled: bool = Field(default=True, alias="CONSOLE_REVIEW_GATE_ENABLED")
    diff_review_timeout_s: float = Field(default=3600.0, alias="DIFF_REVIEW_TIMEOUT_S")
    # How many times the user may send the work back. The next rejection after the last
    # round ends the run rather than looping.
    max_user_rejection_rounds: int = Field(default=3, alias="MAX_USER_REJECTION_ROUNDS")

    # Interpreter / clarify (ADR 0013). The console generates questions with an LLM; when
    # the Work lane is cold we fall back to the deterministic stub rather than making an
    # interactive caller wait out a lane load. Set autostart when latency does not matter.
    clarify_llm_enabled: bool = Field(default=True, alias="CLARIFY_LLM_ENABLED")
    clarify_autostart_lane: bool = Field(default=False, alias="CLARIFY_AUTOSTART_LANE")
    max_clarify_questions: int = Field(default=4, alias="MAX_CLARIFY_QUESTIONS")
    interpreter_timeout_s: float = Field(default=120.0, alias="INTERPRETER_TIMEOUT_S")
    # Qwen3.6 has hybrid thinking on by default. Clarify is interactive, so the reasoning
    # trace is paid for while a human waits — see docs/model-evaluation.md probe E. Turning
    # it on needs the longer deadline too: measured 177s, over the interactive timeout.
    interpreter_thinking_enabled: bool = Field(default=False, alias="INTERPRETER_THINKING_ENABLED")
    interpreter_thinking_timeout_s: float = Field(
        default=600.0, alias="INTERPRETER_THINKING_TIMEOUT_S"
    )
    # Grounded clarify (roadmap M9). The session owns a checkout from creation (M8), so the
    # Interpreter can finally obey its own "never ask what you can look up in the repository
    # context" instruction. Capped because enrich_repo_context returns manifest + grep +
    # pre-search unbounded, and clarify runs against the 120 s interactive deadline above.
    clarify_repo_context_enabled: bool = Field(default=True, alias="CLARIFY_REPO_CONTEXT_ENABLED")
    interpreter_repo_context_chars: int = Field(
        default=12_000, alias="INTERPRETER_REPO_CONTEXT_CHARS"
    )

    # Codebase chat (roadmap M9, ADR 0017). An ask is one read-only Explainer turn on the
    # Work lane. It takes the same single admission slot a run does — not for the queue, but
    # because ensure_lane stops every other lane, so a run starting mid-ask would pull the
    # lane out from under it. No warm-lane policy: a cold ask pays the load and says so on
    # the stream (lane_loading/lane_ready) rather than keeping 23 GB resident between
    # questions, so AGENTS.md §4.1 stands unamended.
    console_ask_enabled: bool = Field(default=True, alias="CONSOLE_ASK_ENABLED")
    max_explainer_turns: int = Field(default=8, alias="MAX_EXPLAINER_TURNS")
    explainer_timeout_s: float = Field(default=300.0, alias="EXPLAINER_TIMEOUT_S")
    # Answer text streams as coalesced answer_delta events: one event per this many
    # characters or per this interval, whichever comes first. A per-token event would be one
    # SQLite row and one bus publish each, and a 2000-token answer would swamp the timeline.
    answer_delta_chars: int = Field(default=120, alias="ANSWER_DELTA_CHARS")
    answer_delta_interval_s: float = Field(default=0.25, alias="ANSWER_DELTA_INTERVAL_S")
    # Citations point at path + line range, so the console serves file contents read-only.
    console_file_max_bytes: int = Field(default=262_144, alias="CONSOLE_FILE_MAX_BYTES")

    # Coder (Laguna S 2.1) sampling — Poolside-recommended defaults. The NVFP4
    # quant degrades on raw/unset sampling, so we always pin these (T=0.7, top_p
    # 0.95, top_k 20 are the eval-certified values from the model card / vLLM recipe).
    coder_temperature: float = Field(default=0.7, alias="CODER_TEMPERATURE")
    coder_top_p: float = Field(default=0.95, alias="CODER_TOP_P")
    coder_top_k: int = Field(default=20, alias="CODER_TOP_K")

    # Reasoning escalation: thinking is off for the first attempts (fast/cheap) and
    # only turned on from attempt >= escalation_attempt, where a longer request
    # timeout absorbs the reasoning trace. Requires --reasoning-parser poolside_v1
    # on the coder lane so the trace is stripped before tool-call parsing.
    coder_thinking_enabled: bool = Field(default=True, alias="CODER_THINKING_ENABLED")
    coder_thinking_escalation_attempt: int = Field(
        default=2, alias="CODER_THINKING_ESCALATION_ATTEMPT"
    )
    coder_request_timeout_s: float = Field(default=600.0, alias="CODER_REQUEST_TIMEOUT_S")
    coder_thinking_timeout_s: float = Field(default=1800.0, alias="CODER_THINKING_TIMEOUT_S")

    # Wall-clock caps on the two subprocess.run calls that previously ran unbounded
    # (acceptance test commands, git). A hung child otherwise blocks the event loop forever.
    acceptance_test_timeout_s: float = Field(default=900.0, alias="ACCEPTANCE_TEST_TIMEOUT_S")
    git_timeout_s: float = Field(default=120.0, alias="GIT_TIMEOUT_S")

    # How long a cancelled run may keep going cooperatively before the RunRegistry
    # hard-cancels its task (M5). A body blocked inside subprocess.run cannot see the
    # token until the child returns, so the honest worst case is this plus the two
    # timeouts above — not this alone.
    cancel_grace_s: float = Field(default=30.0, alias="CANCEL_GRACE_S")

    workspace_base: Path = Field(
        default=Path.home() / "sprint-workspaces",
        alias="SPRINT_WORKSPACE_BASE",
    )
    # Depth 50 rather than 1: the clone is now a session-long reference checkout, and the
    # agents' git_log tool returns almost nothing against a single-commit history.
    git_clone_depth: int = Field(default=50, alias="GIT_CLONE_DEPTH")
    # Console sessions own a clone from creation (roadmap M8). Both switches exist because
    # a repo that cannot be cloned, or an embed sidecar that is down, must not stop a
    # session being created — and because CI wants neither.
    console_workspace_on_create: bool = Field(default=True, alias="CONSOLE_WORKSPACE_ON_CREATE")
    console_index_on_create: bool = Field(default=True, alias="CONSOLE_INDEX_ON_CREATE")
    # Every session is a clone, so disk is the binding constraint. Beyond this, terminal
    # sessions' workspaces are evicted least-recently-used first.
    console_max_workspaces: int = Field(default=10, alias="CONSOLE_MAX_WORKSPACES")
    checkpoint_db: Path = Field(
        default=Path.home() / ".sprint-crew" / "checkpoints.db",
        alias="SPRINT_CHECKPOINT_DB",
    )
    session_db: Path = Field(
        default=Path.home() / ".sprint-crew" / "sessions.db",
        alias="SPRINT_SESSION_DB",
    )
    # Console sessions in a terminal state older than this are reaped (row + workspace)
    # on startup and after each session completes. No scheduler; runs inline.
    console_session_ttl_days: float = Field(default=14.0, alias="CONSOLE_SESSION_TTL_DAYS")

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
    # Repo-keyed collections outlive the runs that built them (roadmap M8), so an LRU cap
    # is what bounds Qdrant instead of the old delete-after-every-run. 0 disables the cap.
    vector_max_collections: int = Field(default=10, alias="VECTOR_MAX_COLLECTIONS")
    # Drop prefixed collections the manifest does not know — session-keyed leftovers from
    # before the re-key, and overlays whose run died before its cleanup ran.
    vector_orphan_sweep: bool = Field(default=True, alias="VECTOR_ORPHAN_SWEEP")

    @property
    def project_root(self) -> Path:
        return Path(__file__).resolve().parents[2]

    @property
    def console_cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.console_cors_origins.split(",") if o.strip()]


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
