"""The entry points the outside world calls. Each is a thin wrapper over the graph.

    handle_inbound_message  <- WhatsApp / SMS / email webhook
    pending_approvals       <- staff approval screen (list)
    decide                  <- staff approval screen (approve / reject)
    fire_due_timeouts       <- scheduler, e.g. every 15 minutes (Phase 4 wires the schedule)
    start / recheck         <- detector (Phase 4)

They work with either the compiled LangGraph or the simulator. An HTTP layer
(FastAPI, etc.) only needs to parse the request and call one of these.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from . import clock
from .graph.build import case_state, pending_interrupt, recheck_case, resume_case, start_case
from .tools import ToolContext


@dataclass
class InboundResult:
    handled: bool
    shipment_id: str | None
    detail: str
    status: str | None = None


class CaseService:
    def __init__(self, graph, ctx: ToolContext):
        self.graph, self.ctx = graph, ctx
        self.known: set[str] = set()  # shipment ids with a thread; Phase 4 moves this to the database

    # ------------------------------------------------------------------ cases
    def start(self, shipment_id: str, carrier: str, order_id: str, trigger: str) -> dict:
        self.known.add(shipment_id)
        return start_case(self.graph, shipment_id, carrier, order_id, trigger)

    def recheck(self, shipment_id: str) -> dict:
        return recheck_case(self.graph, shipment_id)

    def state(self, shipment_id: str) -> dict:
        return case_state(self.graph, shipment_id)

    # ------------------------------------------------------------------ customer replies
    def handle_inbound_message(self, from_contact: str, text: str) -> InboundResult:
        order_id = self.ctx.orders.find_by_contact(from_contact)
        if order_id is None:
            return InboundResult(False, None, "Unknown sender; route to the general support inbox.")
        awb = self.ctx.orders.get(order_id).awb
        waiting = pending_interrupt(self.graph, awb) if awb in self.known else None
        if not waiting or waiting.get("kind") != "customer_reply":
            # Not something we asked about: keep it on the case and let a person see it.
            self.ctx.cases.record_reply(awb, text)
            return InboundResult(False, awb, "No open question for this customer; message stored for support.")
        result = resume_case(self.graph, awb, {"type": "reply", "text": text})
        return InboundResult(True, awb, "Reply applied to case.", result.get("status"))

    def fire_due_timeouts(self, now: datetime | None = None) -> list[str]:
        """Resume every case whose customer reply deadline has passed. Returns the shipment ids fired."""
        now = now or clock.now()
        fired = []
        for awb in sorted(self.known):
            waiting = pending_interrupt(self.graph, awb)
            if waiting and waiting.get("kind") == "customer_reply" and waiting.get("reply_deadline"):
                if datetime.fromisoformat(waiting["reply_deadline"]) <= now:
                    resume_case(self.graph, awb, {"type": "timeout"})
                    fired.append(awb)
        return fired

    # ------------------------------------------------------------------ approvals
    def pending_approvals(self) -> list[dict]:
        out = []
        for awb in sorted(self.known):
            waiting = pending_interrupt(self.graph, awb)
            if waiting and waiting.get("kind") == "approval":
                out.append(waiting)
        return out

    def decide(self, shipment_id: str, approver: str, role: Literal["agent", "manager"],
               approved: bool, note: str = "") -> dict:
        waiting = pending_interrupt(self.graph, shipment_id)
        if not waiting or waiting.get("kind") != "approval":
            raise ValueError(f"{shipment_id} is not waiting for an approval.")
        return resume_case(self.graph, shipment_id,
                           {"approved": approved, "approver": approver, "role": role, "note": note})
