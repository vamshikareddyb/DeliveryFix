"""End-to-end graph behaviour, run on the dependency-free simulator.

test_langgraph_build.py runs the same flows on real LangGraph when installed.
"""
import pytest

from shipment_agent.graph import ScriptedBrain, build_simulated_graph, recheck_case, start_case
from shipment_agent.scenarios import BY_AWB, SCENARIOS
from shipment_agent.tools import ToolContext

EXPECTED = {
    # awb: (final status, node that handled it)
    "DL1000000001": ("closed", "close_case"),
    "DL1000000002": ("monitoring", "notify_delay"),
    "BD2000000003": ("waiting_customer", "ask_reschedule"),
    "DL1000000004": ("waiting_customer", "ask_address"),
    "DL1000000005": ("monitoring", "carrier_inquiry"),
    "BD2000000006": ("awaiting_approval", "propose_resolution"),
    "DL1000000007": ("closed", "close_case"),
    "BD2000000008": ("escalated", "escalate"),
}


def make(overrides=None):
    ctx = ToolContext()
    brain = ScriptedBrain(ctx, overrides)
    return ctx, brain, build_simulated_graph(ctx, brain)


def run(graph, awb, trigger="flagged"):
    sc = BY_AWB[awb]
    return start_case(graph, awb, sc.carrier, sc.order_id, trigger)


@pytest.mark.parametrize("sc", SCENARIOS, ids=lambda s: s.awb)
def test_each_scenario_routes_correctly(sc):
    _, _, g = make()
    s = run(g, sc.awb)
    status, node = EXPECTED[sc.awb]
    assert s["status"] == status
    assert node in s["visited"]
    assert s["visited"][:2] == ["investigate", "diagnose"]


def test_delay_sends_one_message_and_schedules_recheck():
    ctx, _, g = make()
    s = run(g, "DL1000000002")
    assert s["visited"] == ["investigate", "diagnose", "notify_delay", "schedule_recheck"]
    assert s["recheck_count"] == 1 and s["next_check_at"]
    assert len(ctx.outbox.sent) == 1
    assert "running a little late" in ctx.outbox.sent[0]["body"]


def test_recheck_loop_escalates_after_limit():
    ctx, brain, g = make()
    run(g, "DL1000000002")
    for _ in range(2):
        s = recheck_case(g, "DL1000000002")
        assert s["status"] == "monitoring"
    s = recheck_case(g, "DL1000000002")  # 4th run: recheck_count already 3
    assert s["status"] == "escalated"
    assert any("rechecks without resolution" in o for o in s["guardrail_overrides"])
    # Same ETA each time, so the customer got exactly one delay message.
    assert len(ctx.outbox.sent) == 1
    assert ctx.backoffice.tickets[0]["case_id"] == "DL1000000002"


def test_recheck_keeps_identity_from_checkpoint():
    _, brain, g = make()
    run(g, "DL1000000002")
    recheck_case(g, "DL1000000002")
    assert brain.calls.count("investigate:DL1000000002") == 2


def test_lost_inquiry_first_then_refund_proposal():
    # First run: the LLM jumps straight to a refund; guardrails force a carrier inquiry.
    _, _, g = make({"DL1000000005": {"recommended_action": "propose_refund"}})
    s = run(g, "DL1000000005")
    assert "carrier_inquiry" in s["visited"]
    assert any("no carrier inquiry" in o for o in s["guardrail_overrides"])
    assert s["actions"][1]["detail"].startswith("Sent")  # "investigating" message to customer

    # Recheck: inquiry now on record, so the refund proposal is allowed, but only proposed.
    s = recheck_case(g, "DL1000000005")
    assert s["status"] == "awaiting_approval"
    p = s["proposal"]
    assert p["kind"] == "refund" and p["amount"] == 12999 and p["approver_level"] == "manager"


def test_nothing_is_refunded_or_reshipped_in_phase_2():
    ctx, _, g = make()
    for sc in SCENARIOS:
        run(g, sc.awb)
    assert ctx.backoffice.refunds == [] and ctx.backoffice.reshipments == []


def test_high_value_failed_attempt_is_escalated_not_messaged():
    ctx, _, g = make({"BD2000000008": {"issue_type": "failed_attempt", "confidence": 0.95,
                                       "recommended_action": "ask_reschedule"}})
    s = run(g, "BD2000000008")
    assert s["status"] == "escalated"
    assert ctx.outbox.sent == []
    assert ctx.backoffice.tickets[0]["priority"] == "high"
    assert "Guardrails: ask_reschedule -> escalate" in ctx.backoffice.tickets[0]["summary"]


def test_escalation_summary_has_context():
    ctx, _, g = make()
    run(g, "BD2000000008")
    summary = ctx.backoffice.tickets[0]["summary"]
    assert "₹54,999" in summary and "Evidence" in summary


def test_low_confidence_any_issue_escalates():
    _, _, g = make({"BD2000000006": {"confidence": 0.5}})
    assert run(g, "BD2000000006")["status"] == "escalated"


def test_log_records_each_step():
    _, _, g = make()
    s = run(g, "BD2000000003")
    assert [line.split(":")[0].split(" ")[0] for line in s["log"]] == ["investigate", "diagnose", "ask_reschedule"]
