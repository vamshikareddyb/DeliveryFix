"""Runs the flows on real LangGraph. Skipped when langgraph isn't installed."""
import pytest

pytest.importorskip("langgraph")

from shipment_agent.graph import ScriptedBrain, build_graph, recheck_case, start_case  # noqa: E402
from shipment_agent.graph import topology as T  # noqa: E402
from shipment_agent.scenarios import BY_AWB, SCENARIOS  # noqa: E402
from shipment_agent.tools import ToolContext  # noqa: E402

from .test_graph_flow import EXPECTED  # noqa: E402


def make():
    ctx = ToolContext()
    return ctx, build_graph(ctx, ScriptedBrain(ctx))


@pytest.mark.parametrize("sc", SCENARIOS, ids=lambda s: s.awb)
def test_langgraph_matches_simulator(sc):
    _, g = make()
    s = start_case(g, sc.awb, sc.carrier, sc.order_id, "flagged")
    assert s["status"] == EXPECTED[sc.awb][0]


def test_graph_has_all_nodes():
    _, g = make()
    assert set(T.NODES) <= set(g.get_graph().nodes)


def test_checkpoint_persists_between_runs():
    ctx, g = make()
    sc = BY_AWB["DL1000000002"]
    start_case(g, sc.awb, sc.carrier, sc.order_id, "flagged")
    s = recheck_case(g, sc.awb)
    assert s["recheck_count"] == 2 and s["order_id"] == "ORD-0002"
    assert len(s["log"]) >= 8  # appended across both runs
