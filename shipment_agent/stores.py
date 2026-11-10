"""In-memory order store, case store and back-office systems (refunds, reships, helpdesk).

Swap these for real database / service clients later; the tools only depend
on the method signatures.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field

from . import clock
from .config import SETTINGS
from .scenarios import SCENARIOS
from .schemas import ActionResult, Address, CaseHistory, OrderDetails, OrderItem, SentMessage


def mask_phone(phone: str) -> str:
    return "*" * (len(phone) - 4) + phone[-4:]


# --------------------------------------------------------------------------
# Orders
# --------------------------------------------------------------------------
class OrderStore:
    def __init__(self, rows: list[dict] | None = None):
        rows = rows if rows is not None else [s.order for s in SCENARIOS]
        self._rows = {r["order_id"]: r for r in rows}
        self._by_awb = {r["awb"]: r["order_id"] for r in rows}
        self._by_ref = {r["customer_ref"]: r["order_id"] for r in rows}

    def get(self, order_id: str) -> OrderDetails:
        r = self._rows[order_id]
        items = [OrderItem(**i) for i in r["items"]]
        return OrderDetails(
            order_id=r["order_id"],
            awb=r["awb"],
            carrier=r["carrier"],
            customer_ref=r["customer_ref"],
            customer_first_name=r["customer_first_name"],
            preferred_channel=r["preferred_channel"],
            masked_phone=mask_phone(r["phone"]),
            shipping_address=Address(**r["shipping_address"]),
            items=items,
            order_value=sum(i.qty * i.unit_price for i in items),
            payment_mode=r["payment_mode"],
        )

    def order_id_for_awb(self, awb: str) -> str:
        return self._by_awb[awb]

    def contact_for(self, customer_ref: str, channel: str) -> str:
        """Real contact details. Only messaging code calls this; never the LLM."""
        r = self._rows[self._by_ref[customer_ref]]
        return r["email"] if channel == "email" else r["phone"]

    def find_by_contact(self, contact: str) -> str | None:
        """order_id for an inbound phone number or email (phones match on the last 10 digits)."""
        c = contact.strip().lower()
        digits = "".join(ch for ch in c if ch.isdigit())[-10:]
        for r in self._rows.values():
            if c == r["email"].lower() or (digits and r["phone"][-10:] == digits):
                return r["order_id"]
        return None

    def update_address(self, order_id: str, address: Address) -> None:
        self._rows[order_id]["shipping_address"] = address.model_dump()


# --------------------------------------------------------------------------
# Cases
# --------------------------------------------------------------------------
class CaseStore:
    def __init__(self):
        self._cases: dict[str, CaseHistory] = {}

    def get(self, shipment_id: str) -> CaseHistory:
        return self._cases.setdefault(shipment_id, CaseHistory(shipment_id=shipment_id))

    def record_message(self, shipment_id: str, msg: SentMessage) -> None:
        self.get(shipment_id).messages_sent.append(msg)

    def record_reply(self, shipment_id: str, text: str) -> None:
        self.get(shipment_id).customer_replies.append(text)

    def record_action(self, shipment_id: str, action: str) -> None:
        self.get(shipment_id).actions_taken.append(action)

    def record_diagnosis(self, shipment_id: str, diagnosis: dict) -> None:
        self.get(shipment_id).previous_diagnoses.append(diagnosis)


# --------------------------------------------------------------------------
# Back office: refunds, reshipments, helpdesk
# --------------------------------------------------------------------------
class ApprovalRequired(Exception):
    """Raised when a money-moving action is called without a human approval."""


@dataclass
class BackOffice:
    refunds: list[dict] = field(default_factory=list)
    reshipments: list[dict] = field(default_factory=list)
    tickets: list[dict] = field(default_factory=list)

    def issue_refund(self, order_id: str, amount: float, reason: str, approved_by: str | None) -> ActionResult:
        if amount > SETTINGS.refund_hard_cap:
            return ActionResult(ok=False, action="issue_refund", detail=f"Amount exceeds hard cap ₹{SETTINGS.refund_hard_cap:,.0f}.")
        if amount > SETTINGS.refund_auto_approve_limit and not approved_by:
            raise ApprovalRequired(f"Refund of ₹{amount:,.0f} needs human approval.")
        ref = f"RF-{uuid.uuid4().hex[:8]}"
        self.refunds.append(dict(ref=ref, order_id=order_id, amount=amount, reason=reason, approved_by=approved_by, at=clock.now()))
        return ActionResult(ok=True, action="issue_refund", reference=ref, detail=f"Refund ₹{amount:,.0f} initiated.")

    def create_reshipment(self, order_id: str, skus: list[str], approved_by: str | None) -> ActionResult:
        if not approved_by:
            raise ApprovalRequired("Reshipment needs human approval.")
        ref = f"RS-{uuid.uuid4().hex[:8]}"
        self.reshipments.append(dict(ref=ref, order_id=order_id, skus=skus, approved_by=approved_by, at=clock.now()))
        return ActionResult(ok=True, action="create_reshipment", reference=ref, detail="Replacement order created.")

    def escalate(self, case_id: str, summary: str, priority: str) -> ActionResult:
        ref = f"TKT-{len(self.tickets) + 1:05d}"
        self.tickets.append(dict(ref=ref, case_id=case_id, summary=summary, priority=priority, at=clock.now()))
        return ActionResult(ok=True, action="escalate_to_support", reference=ref, detail=f"Ticket {ref} created ({priority}).")
