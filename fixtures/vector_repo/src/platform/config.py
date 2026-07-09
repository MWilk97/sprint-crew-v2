"""Platform configuration."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class PlatformConfig:
    database_path: Path
    ferry_max_attempts: int = 3
    ferry_base_delay_ms: int = 100


def default_config(workspace_root: Path | None = None) -> PlatformConfig:
    root = workspace_root or Path.cwd()
    return PlatformConfig(database_path=root / "data" / "platform.db")
