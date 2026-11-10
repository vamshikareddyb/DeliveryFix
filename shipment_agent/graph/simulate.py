"""A tiny in-process runner for the same topology, with no LangGraph dependency.

It mirrors what the compiled graph does for this flow:
- runs nodes in order and merges updates (appending for APPEND_KEYS),
- follows fixed and conditional edges,
- keeps a per-thread state dict between invocations (stand-in checkpointer),
- supports interrupt(): a waiting node raises, the run stops with
  "__interrupt__" in the result, and resume(value) re-runs that node from the
  top with interrupt() returning `value`, exactly like LangGraph.
Used by the tests and by run_graph.py when LangGraph isn't installed.
"""
from __future__ import annotations

from types import SimpleNamespace

from ..config import SETTINGS, Settings
from ..tools import ToolContext
from . import topology as T
from .brain import Brain
from .nodes import Nodes
from .state import APPEND_KEYS, CaseState

_MISSING = object()


class SimInterrupt(Exception):
    def __init__(self, value):
        super().__init__("interrupted")
        self.value = value


class SimulatedGraph:
    def __init__(self, ctx: ToolContext, brain: Brain, settings: Settings = SETTINGS, max_steps: int = 40,
                 store=None):
        self.nodes = Nodes(ctx, brain, settings, interrupt=self._interrupt)
        self.threads: dict[str, dict] = {}
        self.store = store  # e.g. SqliteThreadStore: makes threads survive a restart
        self.max_steps = max_steps
        self._edges = dict(T.EDGES)
        self._resume_value = _MISSING

    # ---- interrupt plumbing
    def _interrupt(self, payload):
        if self._resume_value is not _MISSING:
            value, self._resume_value = self._resume_value, _MISSING
            return value
        raise SimInterrupt(payload)

    def _merge(self, state: dict, update: dict) -> None:
        for k, v in update.items():
            if k in APPEND_KEYS:
                state[k] = state.get(k, []) + list(v)
            else:
                state[k] = v

    def _run(self, state: dict, start: str) -> dict:
        path, node = [], start
        state.pop("__interrupt__", None)
        for _ in range(self.max_steps):
            path.append(node)
            try:
                update = getattr(self.nodes, node)(state)
            except SimInterrupt as i:
                state["__pending__"] = node
                state["__interrupt__"] = [SimpleNamespace(value=i.value)]
                break
            self._merge(state, update)
            if node in T.ROUTES:
                nxt = getattr(self.nodes, T.ROUTES[node][0])(state)
            else:
                nxt = self._edges[node]
            if nxt == "END":
                state.pop("__pending__", None)
                break
            node = nxt
        else:
            raise RuntimeError(f"Exceeded {self.max_steps} steps: {path}")
        state["visited"] = path  # path of the latest run (simulator-only field)
        return state

    # ---- persistence
    def _load(self, tid: str) -> dict:
        if tid not in self.threads and self.store is not None:
            saved = self.store.load(tid)
            if saved is not None:
                if saved.get("__interrupt__"):
                    saved["__interrupt__"] = [SimpleNamespace(value=v) for v in saved["__interrupt__"]]
                self.threads[tid] = saved
        return self.threads.setdefault(tid, {})

    def _save(self, tid: str, state: dict) -> None:
        if self.store is not None:
            out = dict(state)
            if out.get("__interrupt__"):
                out["__interrupt__"] = [i.value for i in out["__interrupt__"]]
            self.store.save(tid, out)

    # ---- public API (same shape as a compiled LangGraph)
    def invoke(self, inp: dict, config: dict) -> CaseState:
        tid = config["configurable"]["thread_id"]
        state = self._load(tid)
        state.pop("__pending__", None)  # a fresh run abandons any pending wait
        self._merge(state, inp)
        try:
            return self._run(state, T.ENTRY)
        finally:
            self._save(tid, state)

    def resume(self, value, config: dict) -> CaseState:
        tid = config["configurable"]["thread_id"]
        state = self._load(tid)
        node = state.get("__pending__")
        if node is None:
            raise ValueError("Thread is not waiting on an interrupt.")
        self._resume_value = value
        try:
            return self._run(state, node)
        finally:
            self._resume_value = _MISSING
            self._save(tid, state)

    def get_state(self, config: dict):
        state = self._load(config["configurable"]["thread_id"])
        pending = state.get("__pending__")
        values = {k: v for k, v in state.items() if not k.startswith("__")}
        tasks = (SimpleNamespace(interrupts=state.get("__interrupt__", [])),) if pending else ()
        return SimpleNamespace(values=values, next=(pending,) if pending else (), tasks=tasks)


def build_simulated_graph(ctx: ToolContext, brain: Brain, settings: Settings = SETTINGS,
                          store=None) -> SimulatedGraph:
    return SimulatedGraph(ctx, brain, settings, store=store)
