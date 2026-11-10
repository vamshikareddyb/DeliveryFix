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


def test_interrupt_and_resume_customer_reply():
    from shipment_agent.graph import pending_interrupt, resume_case

    ctx, g = make()
    sc = BY_AWB["BD2000000003"]
    out = start_case(g, sc.awb, sc.carrier, sc.order_id, "flagged")
    assert "__interrupt__" in out
    assert pending_interrupt(g, sc.awb)["kind"] == "customer_reply"
    s = resume_case(g, sc.awb, {"type": "reply", "text": "tomorrow evening"})
    assert s["status"] == "monitoring"
    assert ctx.carriers["bluedart"].calls[-1][0] == "request_reattempt"
    # The ask message was sent once even though wait_for_customer re-ran on resume.
    assert sum("couldn't deliver" in m["body"] for m in ctx.outbox.sent) == 1


def test_interrupt_and_resume_approval():
    from shipment_agent.service import CaseService

    ctx, g = make()
    svc = CaseService(g, ctx)
    sc = BY_AWB["BD2000000006"]
    svc.start(sc.awb, sc.carrier, sc.order_id, "flagged")
    assert svc.pending_approvals()[0]["proposal"]["kind"] == "reship"
    s = svc.decide(sc.awb, "agent.rao", "agent", approved=True)
    assert s["status"] == "closed" and ctx.backoffice.reshipments


def test_monitor_cycle_on_langgraph():
    from shipment_agent.scheduler import Monitor, register_scenarios
    from shipment_agent.service import CaseService
    from shipment_agent.tracking_db import TrackingDB

    ctx, g = make()
    db = TrackingDB()
    register_scenarios(db, SCENARIOS)
    m = Monitor(CaseService(g, ctx, db))
    assert len(m.run_cycle().started) == 6
    assert m.run_cycle().started == []


def test_sqlite_checkpointer_survives_restart(tmp_path):
    pytest.importorskip("langgraph.checkpoint.sqlite")
    from shipment_agent.graph import pending_interrupt, resume_case
    from shipment_agent.graph.persist import make_checkpointer

    path = str(tmp_path / "cp.db")
    ctx = ToolContext()
    g1 = build_graph(ctx, ScriptedBrain(ctx), checkpointer=make_checkpointer(path))
    sc = BY_AWB["BD2000000003"]
    start_case(g1, sc.awb, sc.carrier, sc.order_id, "flagged")
    g2 = build_graph(ctx, ScriptedBrain(ctx), checkpointer=make_checkpointer(path))  # "restart"
    assert pending_interrupt(g2, sc.awb)["kind"] == "customer_reply"
    assert resume_case(g2, sc.awb, {"type": "reply", "text": "tomorrow evening"})["status"] == "monitoring"
