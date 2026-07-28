"""LangGraph sprint pipeline: node wiring and the run entrypoint.

Node bodies live in ``graph/nodes/`` — one module per stage. This file is the assembly
point: it declares the graph shape and owns the checkpointer/stream loop.
"""

from __future__ import annotations

from typing import Any

from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from sprint_crew.config import get_settings
from sprint_crew.graph.nodes._support import _phased
from sprint_crew.graph.nodes.code import code_implement
from sprint_crew.graph.nodes.flow import (
    awaiting_human,
    failed,
    route_after_diff_review,
    route_after_gate,
    route_after_plan,
    route_after_retry,
)
from sprint_crew.graph.nodes.plan import init_session, tech_lead_plan
from sprint_crew.graph.nodes.retry import prepare_rejection_retry, prepare_retry
from sprint_crew.graph.nodes.review import await_diff_review, merge_gate, review
from sprint_crew.graph.nodes.test import test_implement
from sprint_crew.graph.state import SprintState
from sprint_crew.orchestrator.ship_cycle import orchestrator_ship


def build_sprint_graph(*, checkpointer: Any | None = None) -> CompiledStateGraph:
    graph: StateGraph = StateGraph(SprintState)
    # Phase-bracketed nodes: the ones that do real work and can run for minutes.
    # ``awaitingHuman`` and ``failed`` are terminal bookkeeping that already emit their own
    # event, so a phase pair around them would only add noise.
    for name, node in (
        ("initSession", init_session),
        ("techLeadPlan", tech_lead_plan),
        ("codeImplement", code_implement),
        ("testImplement", test_implement),
        ("review", review),
        ("mergeGate", merge_gate),
        ("awaitDiffReview", await_diff_review),
        ("prepareRetry", prepare_retry),
        ("prepareRejectionRetry", prepare_rejection_retry),
        ("orchestratorShip", orchestrator_ship),
    ):
        graph.add_node(name, _phased(name, node))
    graph.add_node("awaitingHuman", awaiting_human)
    graph.add_node("failed", failed)

    graph.add_edge(START, "initSession")
    graph.add_edge("initSession", "techLeadPlan")
    graph.add_conditional_edges(
        "techLeadPlan",
        route_after_plan,
        {
            "code": "codeImplement",
            "failed": "failed",
        },
    )
    graph.add_edge("codeImplement", "testImplement")
    graph.add_edge("testImplement", "review")
    graph.add_edge("review", "mergeGate")
    # The deterministic gate never ships directly any more: an accepted change goes to the
    # human review gate first (M7, ADR 0015). That node is a pass-through outside a console
    # run, so the from-ticket and smoke paths are unchanged.
    graph.add_conditional_edges(
        "mergeGate",
        route_after_gate,
        {
            "review": "awaitDiffReview",
            "retry": "prepareRetry",
            "failed": "failed",
        },
    )
    graph.add_conditional_edges(
        "awaitDiffReview",
        route_after_diff_review,
        {
            "ship": "orchestratorShip",
            "retry": "prepareRejectionRetry",
            "failed": "failed",
        },
    )
    for retry_node in ("prepareRetry", "prepareRejectionRetry"):
        graph.add_conditional_edges(
            retry_node,
            route_after_retry,
            {
                "plan": "techLeadPlan",
                "code": "codeImplement",
                "failed": "failed",
            },
        )
    graph.add_edge("orchestratorShip", "awaitingHuman")
    graph.add_edge("awaitingHuman", END)
    graph.add_edge("failed", END)

    return graph.compile(checkpointer=checkpointer)


async def run_sprint_cycle(
    state: SprintState,
    *,
    on_node_complete: Any | None = None,
) -> SprintState:
    settings = get_settings()
    settings.checkpoint_db.parent.mkdir(parents=True, exist_ok=True)
    async with AsyncSqliteSaver.from_conn_string(str(settings.checkpoint_db)) as checkpointer:
        app = build_sprint_graph(checkpointer=checkpointer)
        config = {"configurable": {"thread_id": state["session_id"]}}
        accumulated: dict[str, Any] = dict(state)
        async for chunk in app.astream(state, config, stream_mode="updates"):
            for _node_name, update in chunk.items():
                if not isinstance(update, dict):
                    continue
                for key, value in update.items():
                    if key == "events" and key in accumulated and isinstance(value, list):
                        accumulated["events"] = list(accumulated.get("events", [])) + value
                    else:
                        accumulated[key] = value
                if on_node_complete is not None:
                    await on_node_complete(dict(accumulated))
        return accumulated  # type: ignore[return-value]
