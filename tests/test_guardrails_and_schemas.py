import pytest
from pydantic import ValidationError

from shipment_agent.carriers.mock import MockCarrierAdapter
from shipment_agent.guardrails import apply_guardrails
from shipment_agent.schemas import CaseHistory, CustomerReplyParse, Diagnosis
from shipment_agent.stores import OrderStore

orders = OrderStore()


def diag(**kw):
    base = dict(issue_type="delayed", severity="low", confidence=0.9, evidence=["no scan 60h"],
                recommended_action="notify_delay", summary_for_human="Delayed.")
    return Diagnosis(**{**base, **kw})


def test_diagnosis_rejects_free_text_action():
    with pytest.raises(ValidationError):
        diag(recommended_action="send a nice apology")


def test_diagnosis_requires_evidence():
    with pytest.raises(ValidationError):
        diag(evidence=[])


def test_reply_parse_validates_pincode():
    with pytest.raises(ValidationError):
        CustomerReplyParse(intent="new_address", confidence=0.9, needs_human=False,
                           address=dict(line1="x", city="Mumbai", state="MH", pincode="4000"))


def test_low_confidence_escalates():
    d, why = apply_guardrails(diag(confidence=0.4), orders.get("ORD-0002"), None)
    assert d.recommended_action == "escalate" and why


def test_high_value_escalates_unless_delay():
    o = orders.get("ORD-0008")  # ₹54,999 laptop
    d, _ = apply_guardrails(diag(issue_type="failed_attempt", recommended_action="ask_reschedule"), o, None)
    assert d.recommended_action == "escalate"
    d, _ = apply_guardrails(diag(), o, None)
    assert d.recommended_action == "notify_delay"


def test_lost_needs_inquiry_before_refund():
    t = MockCarrierAdapter("delhivery").get_tracking("DL1000000005")
    o = orders.get("ORD-0005")
    d, _ = apply_guardrails(diag(issue_type="lost", recommended_action="propose_refund"), o, t)
    assert d.recommended_action == "carrier_inquiry"
    h = CaseHistory(shipment_id="DL1000000005", actions_taken=["inquiry:INQ-1"])
    d, _ = apply_guardrails(diag(issue_type="lost", recommended_action="propose_refund"), o, t, h)
    assert d.recommended_action == "propose_refund"


def test_lost_downgraded_when_scan_is_recent():
    t = MockCarrierAdapter("delhivery").get_tracking("DL1000000002")  # 60h stale
    d, why = apply_guardrails(diag(issue_type="lost", recommended_action="carrier_inquiry"), orders.get("ORD-0002"), t)
    assert d.issue_type == "delayed" and d.recommended_action == "notify_delay"


def test_no_refund_on_cod():
    d, _ = apply_guardrails(diag(issue_type="damaged", recommended_action="propose_refund"), orders.get("ORD-0003"), None)
    assert d.recommended_action == "escalate"
