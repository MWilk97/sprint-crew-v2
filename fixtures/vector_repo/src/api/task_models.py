"""Task domain model."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class Task:
    id: str
    title: str
    done: bool = False
