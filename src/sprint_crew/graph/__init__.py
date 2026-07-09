from __future__ import annotations

from sprint_crew.graph.state import SprintState

__all__ = ["SprintState", "build_sprint_graph", "run_sprint_cycle"]


def __getattr__(name: str):
    if name in __all__ and name != "SprintState":
        from sprint_crew.graph.pipeline import build_sprint_graph, run_sprint_cycle

        return {"build_sprint_graph": build_sprint_graph, "run_sprint_cycle": run_sprint_cycle}[
            name
        ]
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
