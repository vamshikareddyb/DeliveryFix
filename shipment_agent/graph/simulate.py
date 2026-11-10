"""A tiny in-process runner for the same topology, with no LangGraph dependency.

It mirrors what the compiled graph does for this flow: run nodes in order,
merge updates (appending for APPEND_KEYS), follow the conditional edge, and
keep a per-thread state dict between invocations (a stand-in checkpointer).
Used by the tests and by run_graph.py when LangGraph isn't installed.
"""
from __future__ import annotations

from ..config import SETTINGS, Settings
from ..tools import ToolContext
from . import topology as T
from .brain import Brain
from .nodes import Nodes
from .state import APPEND_KEYS, CaseState


class SimulatedGraph:
    def __init__(self, ctx: ToolContext, brain: Brain, settings: Settings = SETTINGS, max_steps: int = 25):
        self.nodes = Nodes(ctx, brain, settings)
        self.threads: dict[str, CaseState] = {}
        self.max_steps = max_steps
        self._edges = dict(T.EDGES)

    def _merge(self, state: dict, update: dict) -> None:
        for k, v in update.items():
            if k in APPEND_KEYS:
                state[k] = state.get(k, []) + list(v)
            else:
                state[k] = v

    def invoke(self, inp: dict, config: dict) -> CaseState:
        tid = config["configurable"]["thread_id"]
        state = self.threads.setdefault(tid, {})
        self._merge(state, inp)
        state.setdefault("visited", [])
        path, node = [], T.ENTRY
        for _ in range(self.max_steps):
            path.append(node)
            self._merge(state, getattr(self.nodes, node)(state))
            nxt = self.nodes.route_after_diagnosis(state) if node == T.ROUTE_FROM else self._edges[node]
            if nxt == "END":
                break
            node = nxt
        else:
            raise RuntimeError(f"Exceeded {self.max_steps} steps: {path}")
        state["visited"] = path  # path of the latest run (simulator-only field)
        return state


def build_simulated_graph(ctx: ToolContext, brain: Brain, settings: Settings = SETTINGS) -> SimulatedGraph:
    return SimulatedGraph(ctx, brain, settings)
