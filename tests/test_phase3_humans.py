"""Phase 3: customer replies, reminders/timeouts and staff approvals (simulator)."""
from datetime import timedelta

import pytest

from shipment_agent import clock
from shipment_agent.graph import ScriptedBrain, build_simulated_graph, pending_interrupt
from shipment_agent.scenarios import BY_AWB
from shipment_agent.service import CaseService
from shipment_agent.tools import ToolContext

from .conftest import NOW


def make(overrides=None):
    ctx = ToolContext()
    g = build_simulated_graph(ctx, ScriptedBrain(ctx, overrides))
    return ctx, g, CaseService(g, ctx)


def start(svc, awb):
    sc = BY_AWB[awb]
    return svc.start(awb, sc.carrier, sc.order_id, "flagged")


def phone(ctx, awb):
    return ctx.orders.contact_for(ctx.orders.get(BY_AWB[awb].order_id).customer_ref, "sms")


# ---------------------------------------------------------------- pausing
def test_ask_pauses_with_payload():
    ctx, g, svc = make()
    s = start(svc, "BD2000000003")
    assert s["status"] == "waiting_customer"
    p = pending_interrupt(g, "BD2000000003")
    assert p["kind"] == "customer_reply" and p["awaiting"] == "reschedule"
    assert p["reply_deadline"] == (NOW + timedelta(hours=24)).isoformat(timespec="minutes")


# ---------------------------------------------------------------- replies
def test_reschedule_reply_rebooks_and_confirms():
    ctx, g, svc = make()
    start(svc, "BD2000000003")
    r = svc.handle_inbound_message("+91 99000 11122", "Please come tomorrow evening")
    assert r.handled and r.status == "monitoring"
    call = ctx.carriers["bluedart"].calls[-1]
    assert call[0] == "request_reattempt" and call[1]["slot"] == {"date": "2026-11-11", "window": "evening"}
    assert "will be delivered on Wed, 11 Nov (evening)" in ctx.outbox.sent[-1]["body"]
    assert pending_interrupt(g, "BD2000000003") is None


def test_address_reply_updates_carrier_and_order():
    ctx, g, svc = make()
    start(svc, "DL1000000004")
    r = svc.handle_inbound_message(phone(ctx, "DL1000000004"),
                                   "Flat 3B, Sea Breeze Apartments, Marine Drive, 400020")
    assert r.status == "monitoring"
    assert ctx.orders.get("ORD-0004").shipping_address.pincode == "400020"
    assert "updated the delivery address" in ctx.outbox.sent[-1]["body"]


def test_carrier_refusing_address_change_escalates_for_rto():
    ctx, g, svc = make({"BD2000000003": {"issue_type": "wrong_address", "recommended_action": "ask_address"}})
    start(svc, "BD2000000003")
    r = svc.handle_inbound_message(phone(ctx, "BD2000000003"), "B-14 Saket, Block C, New Delhi 110017")
    assert r.status == "escalated"
    summary = ctx.backoffice.tickets[-1]["summary"]
    assert "does not allow address changes" in summary and "RTO" in summary


@pytest.mark.parametrize("text,reason", [
    ("This is the worst service, I want a refund", "needs a person"),
    ("I'll pick it up myself, PICKUP", "pickup point"),
    ("cancel the order", "cancel"),
    ("what is happening??", "needs a person"),
])
def test_replies_that_need_a_person(text, reason):
    ctx, g, svc = make()
    start(svc, "BD2000000003")
    r = svc.handle_inbound_message(phone(ctx, "BD2000000003"), text)
    assert r.status == "escalated"
    summary = ctx.backoffice.tickets[-1]["summary"]
    assert reason in summary and f"Customer said: {text!r}" in summary
    assert ctx.carriers["bluedart"].calls == []  # nothing rebooked


def test_slot_too_far_ahead_escalates():
    ctx, g, svc = make()
    start(svc, "BD2000000003")
    r = svc.handle_inbound_message(phone(ctx, "BD2000000003"), "deliver on 2026-12-20 please")
    assert r.status == "escalated"
    assert "outside the allowed window" in ctx.backoffice.tickets[-1]["summary"]


def test_pre_parsed_reply_skips_the_parser():
    ctx, g, svc = make()
    brain = g.nodes.brain
    start(svc, "BD2000000003")
    from shipment_agent.graph import resume_case
    s = resume_case(g, "BD2000000003", {"type": "reply", "text": "[button] Sat morning", "parsed": {
        "intent": "reschedule", "slot": {"date": "2026-11-14", "window": "morning"},
        "confidence": 1.0, "needs_human": False}})
    assert s["status"] == "monitoring" and "parse_reply" not in brain.calls


# ---------------------------------------------------------------- inbound edge cases
def test_unknown_sender():
    _, _, svc = make()
    r = svc.handle_inbound_message("9000000000", "hello")
    assert not r.handled and r.shipment_id is None


def test_message_when_not_waiting_is_stored_not_applied():
    ctx, g, svc = make()
    start(svc, "DL1000000002")  # delay case, monitoring, no question asked
    r = svc.handle_inbound_message(phone(ctx, "DL1000000002"), "where is my order?")
    assert not r.handled
    assert ctx.cases.get("DL1000000002").customer_replies == ["where is my order?"]


# ---------------------------------------------------------------- timeouts and reminders
def test_timeouts_fire_only_after_deadline():
    ctx, g, svc = make()
    start(svc, "DL1000000004")
    assert svc.fire_due_timeouts() == []
    assert svc.fire_due_timeouts(NOW + timedelta(hours=25)) == ["DL1000000004"]


def test_first_timeout_sends_reminder_and_waits_again():
    ctx, g, svc = make()
    start(svc, "DL1000000004")
    clock.freeze(NOW + timedelta(hours=25))
    svc.fire_due_timeouts()
    assert "quick reminder" in ctx.outbox.sent[-1]["body"]
    p = pending_interrupt(g, "DL1000000004")
    assert p["reminders_sent"] == 1
    assert p["reply_deadline"] == (NOW + timedelta(hours=49)).isoformat(timespec="minutes")


def test_no_reply_prepaid_falls_back_to_carrier_reattempt():
    ctx, g, svc = make()
    start(svc, "DL1000000004")  # prepaid
    clock.freeze(NOW + timedelta(hours=25)); svc.fire_due_timeouts()
    clock.freeze(NOW + timedelta(hours=50)); svc.fire_due_timeouts()
    s = svc.state("DL1000000004")
    assert s["status"] == "monitoring" and s["next_check_at"]
    assert ctx.backoffice.tickets == []


def test_no_reply_cod_escalates():
    ctx, g, svc = make()
    start(svc, "BD2000000003")  # COD
    clock.freeze(NOW + timedelta(hours=25)); svc.fire_due_timeouts()
    clock.freeze(NOW + timedelta(hours=50)); svc.fire_due_timeouts()
    assert svc.state("BD2000000003")["status"] == "escalated"
    assert "COD" in ctx.backoffice.tickets[-1]["summary"]


def test_reply_after_reminder_still_works():
    ctx, g, svc = make()
    start(svc, "BD2000000003")
    clock.freeze(NOW + timedelta(hours=25)); svc.fire_due_timeouts()
    r = svc.handle_inbound_message(phone(ctx, "BD2000000003"), "friday afternoon")
    assert r.status == "monitoring"


# ---------------------------------------------------------------- approvals
def test_reship_approved_by_agent_executes_and_closes():
    ctx, g, svc = make()
    start(svc, "BD2000000006")
    [pending] = svc.pending_approvals()
    assert pending["proposal"]["kind"] == "reship" and pending["proposal"]["approver_level"] == "agent"
    s = svc.decide("BD2000000006", "agent.rao", "agent", approved=True)
    assert s["status"] == "closed"
    assert ctx.backoffice.reshipments[0]["approved_by"] == "agent.rao"
    assert "free replacement" in ctx.outbox.sent[-1]["body"]
    assert svc.pending_approvals() == []


def test_rejection_escalates_with_note():
    ctx, g, svc = make()
    start(svc, "BD2000000006")
    s = svc.decide("BD2000000006", "agent.rao", "agent", approved=False, note="Photos show packaging only")
    assert s["status"] == "escalated"
    assert ctx.backoffice.reshipments == []
    assert "rejected by agent.rao: Photos show packaging only" in ctx.backoffice.tickets[-1]["summary"]


def test_manager_level_refund_needs_manager():
    ctx, g, svc = make({"DL1000000005": {"recommended_action": "propose_refund"}})
    start(svc, "DL1000000005")       # guardrails force carrier inquiry first
    svc.recheck("DL1000000005")       # inquiry on record -> refund proposal, ₹12,999
    [pending] = svc.pending_approvals()
    assert pending["proposal"]["approver_level"] == "manager"

    s = svc.decide("DL1000000005", "agent.rao", "agent", approved=True)
    assert s["status"] == "awaiting_approval" and ctx.backoffice.refunds == []
    assert "needs a manager" in pending_interrupt(g, "DL1000000005")["previous_error"]

    s = svc.decide("DL1000000005", "mgr.iyer", "manager", approved=True)
    assert s["status"] == "closed"
    assert ctx.backoffice.refunds[0]["amount"] == 12999 and ctx.backoffice.refunds[0]["approved_by"] == "mgr.iyer"
    assert "refund of ₹12,999" in ctx.outbox.sent[-1]["body"]


def test_decide_requires_a_pending_approval():
    _, _, svc = make()
    start(svc, "DL1000000002")
    with pytest.raises(ValueError):
        svc.decide("DL1000000002", "agent.rao", "agent", approved=True)


def test_new_run_abandons_stale_wait():
    ctx, g, svc = make()
    start(svc, "BD2000000003")
    svc.recheck("BD2000000003")  # detector re-flags before any reply
    # Same question isn't sent twice (messaging is idempotent per case).
    assert sum("couldn't deliver" in m["body"] for m in ctx.outbox.sent) == 1
    assert pending_interrupt(g, "BD2000000003")["kind"] == "customer_reply"
