"""Pytest bootstrap for story-3-clean vector_repo overlay.

Removes stdlib ``platform`` shadow so ``from platform.config`` resolves to
``src/platform/``. Applied via copy_vector_fixture(..., overlay="story3_clean").
"""

from __future__ import annotations

import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))
sys.modules.pop("platform", None)
