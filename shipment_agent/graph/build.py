"""Builds the LangGraph StateGraph from the shared topology, plus run helpers
that work the same on the compiled graph and on the simulator."""
from __future__ import annotations

from typing import Any

from ..config import SETTINGS, Settings
from ..tools import ToolContext
from . import topology as T
from .brain import Brain
from .nodes import Nodes
from .state import CaseState


def build_graph(ctx: ToolContext, brain: Brain, checkpointer=None, settings: Settings = SETTINGS):
    """Compile the case graph.

    A checkpointer is required: interrupts (waiting for a customer or an
    approver) only work when the paused state is saved. Defaults to in-memory.
    For cases that must survive restarts use a persistent one, e.g.
        from langgraph.checkpoint.sqlite import SqliteSaver   # pip install langgraph-checkpoint-sqlite
    or the Postgres saver in production.
    """
    from langgraph.graph import END, START, StateGraph
    from langgraph.types import interrupt

    if checkpointer is None:
        from langgraph.checkpoint.memory import InMemorySaver

        checkpointer = InMemorySaver()

    nodes = Nodes(ctx, brain, settings, interrupt=interrupt)
    g = StateGraph(CaseState)
    for name in T.NODES:
        g.add_node(name, getattr(nodes, name))

    g.add_edge(START, T.ENTRY)
    for src, dst in T.EDGES:
        g.add_edge(src, END if dst == "END" else dst)
    for src, (router, targets) in T.ROUTES.items():
        g.add_conditional_edges(src, getattr(nodes, router), targets)

    return g.compile(checkpointer=checkpointer)


def thread_config(shipment_id: str) -> dict:
    """One thread per shipment, so reruns, rechecks and resumes share persisted state."""
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


def resume_case(graph, shipment_id: str, value: Any) -> dict:
    """Answer a pending interrupt (customer reply, timeout, or approval decision)."""
    from .simulate import SimulatedGraph

    if isinstance(graph, SimulatedGraph):
        return graph.resume(value, thread_config(shipment_id))
    from langgraph.types import Command

    return graph.invoke(Command(resume=value), thread_config(shipment_id))


def pending_interrupt(graph, shipment_id: str) -> dict | None:
    """The payload the case is waiting on, or None if it isn't paused."""
    snap = graph.get_state(thread_config(shipment_id))
    for task in getattr(snap, "tasks", ()) or ():
        for intr in getattr(task, "interrupts", ()) or ():
            return intr.value
    return None


def case_state(graph, shipment_id: str) -> CaseState:
    return graph.get_state(thread_config(shipment_id)).values
