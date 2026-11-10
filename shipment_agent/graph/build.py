"""Builds the LangGraph StateGraph from the shared topology."""
from __future__ import annotations

from ..config import SETTINGS, Settings
from ..tools import ToolContext
from . import topology as T
from .brain import Brain
from .nodes import Nodes
from .state import CaseState


def build_graph(ctx: ToolContext, brain: Brain, checkpointer=None, settings: Settings = SETTINGS):
    """Compile the case graph.

    checkpointer: any LangGraph checkpointer. Defaults to in-memory. For cases that
    must survive restarts use a persistent one, e.g.
        from langgraph.checkpoint.sqlite import SqliteSaver   # pip install langgraph-checkpoint-sqlite
        build_graph(ctx, brain, checkpointer=SqliteSaver.from_conn_string("cases.db"))
    or the Postgres saver in production.
    """
    from langgraph.graph import END, START, StateGraph

    if checkpointer is None:
        from langgraph.checkpoint.memory import InMemorySaver

        checkpointer = InMemorySaver()

    nodes = Nodes(ctx, brain, settings)
    g = StateGraph(CaseState)
    for name in T.NODES:
        g.add_node(name, getattr(nodes, name))

    g.add_edge(START, T.ENTRY)
    for src, dst in T.EDGES:
        g.add_edge(src, END if dst == "END" else dst)
    g.add_conditional_edges(T.ROUTE_FROM, nodes.route_after_diagnosis, T.ROUTE_TARGETS)

    return g.compile(checkpointer=checkpointer)


def thread_config(shipment_id: str) -> dict:
    """One thread per shipment, so reruns and rechecks share persisted state."""
    return {"configurable": {"thread_id": f"shipment:{shipment_id}"}}


def start_case(graph, shipment_id: str, carrier: str, order_id: str, trigger: str) -> dict:
    return graph.invoke(
        {"shipment_id": shipment_id, "carrier": carrier, "order_id": order_id, "trigger": trigger,
         "recheck_count": 0},
        thread_config(shipment_id),
    )


def recheck_case(graph, shipment_id: str) -> dict:
    """Re-enter an existing thread; identity and history come from the checkpoint."""
    return graph.invoke({"trigger": "recheck"}, thread_config(shipment_id))
