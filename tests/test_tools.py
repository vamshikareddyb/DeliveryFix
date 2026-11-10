import pytest

from shipment_agent.carriers.mock import MockCarrierAdapter
from shipment_agent.schemas import Address, DeliverySlot
from shipment_agent.stores import ApprovalRequired


# ---------------- read tools ----------------
def test_order_details_are_masked(read):
    o = read.get_order_details("ORD-0003")
    assert o["masked_phone"] == "******1122"
    assert "phone" not in o and "email" not in o
    assert o["order_value"] == 1599 and o["payment_mode"] == "cod"


def test_tracking_tool_returns_json_safe_dict(read):
    t = read.get_tracking_status("DL1000000004", "delhivery")
    assert t["current_status"] == "delivery_failed"
    assert isinstance(t["last_scan_at"], str)


def test_carrier_notes(read):
    n = read.get_carrier_notes("DL1000000004", "delhivery")
    assert any("Address incomplete" in x for x in n["notes"])


def test_tool_errors_are_returned_not_raised(ctx, read):
    assert "error" in read.get_order_details("ORD-9999")
    ctx.carriers["delhivery"] = MockCarrierAdapter("delhivery", failure_rate=1.0, seed=0)
    r = read.get_tracking_status("DL1000000001", "delhivery")
    assert r["retryable"] is True


def test_policy_search_with_carrier_filter(read):
    hits = read.search_policy("can the address be changed in transit", carrier="bluedart")["results"]
    ids = [h["chunk_id"] for h in hits]
    assert ids[0] == "bluedart-caps"
    assert "delhivery-caps" not in ids


def test_policy_search_refund(read):
    hits = read.search_policy("refund approval above 5000")["results"]
    assert hits[0]["chunk_id"] == "refund-limits"


# ---------------- messaging ----------------
def test_message_uses_template_and_is_idempotent(ctx, act):
    args = dict(shipment_id="BD2000000003", customer_ref="CUST-0003", channel="whatsapp",
                template_id="reschedule_request", variables={"first_name": "Priya", "order_id": "ORD-0003"})
    r1 = act.send_customer_message(**args)
    r2 = act.send_customer_message(**args)
    assert r1.ok and r1.reference
    assert "skipped duplicate" in r2.detail
    assert len(ctx.outbox.sent) == 1
    assert ctx.outbox.sent[0]["to"] == "9900011122"  # real number only reaches the gateway


def test_message_missing_variable(act):
    r = act.send_customer_message("BD2000000003", "CUST-0003", "sms", "delay_update", {"first_name": "Priya"})
    assert not r.ok and "Missing" in r.detail


def test_unknown_template_rejected(act):
    r = act.send_customer_message("BD2000000003", "CUST-0003", "sms", "free_text", {})
    assert not r.ok


# ---------------- carrier actions ----------------
def test_rebooking_and_limit(act):
    slot = DeliverySlot(date="2026-11-11", window="evening")
    assert act.request_rebooking("BD2000000003", "bluedart", slot).ok
    assert act.request_rebooking("BD2000000003", "bluedart", slot).ok
    assert not act.request_rebooking("BD2000000003", "bluedart", slot).ok  # bluedart max 2


def test_address_change_respects_carrier_capability(ctx, act):
    addr = Address(line1="Flat 3B, Sea Breeze, Marine Drive", city="Mumbai", state="Maharashtra", pincode="400020")
    assert act.update_delivery_address("DL1000000004", "delhivery", addr).ok
    assert ctx.orders.get("ORD-0004").shipping_address.pincode == "400020"
    assert not act.update_delivery_address("BD2000000003", "bluedart", addr).ok


# ---------------- money ----------------
def test_refund_requires_approval(ctx, act):
    with pytest.raises(ApprovalRequired):
        act.issue_refund("DL1000000005", "ORD-0005", 12999, "lost", approved_by=None)
    assert act.issue_refund("DL1000000005", "ORD-0005", 12999, "lost", approved_by="agent.rao").ok
    assert "refund:" in ctx.cases.get("DL1000000005").actions_taken[0]


def test_reship_requires_approval(act):
    with pytest.raises(ApprovalRequired):
        act.create_reshipment("BD2000000006", "ORD-0006", ["LM-01"], approved_by=None)


def test_escalation_ticket(ctx, act):
    r = act.escalate_to_support("BD2000000008", "Unmapped status on laptop order", "high")
    assert r.reference == "TKT-00001"
    assert ctx.backoffice.tickets[0]["priority"] == "high"
