"""The entry points the outside world calls. Each is a thin wrapper over the graph.

    handle_inbound_message  <- WhatsApp / SMS / email webhook
    on_carrier_event        <- carrier tracking webhook (after ingest): wakes affected cases
    pending_approvals       <- staff approval screen (list)
    decide                  <- staff approval screen (approve / reject)
    fire_due_timeouts       <- scheduler
    run_due_rechecks        <- scheduler
    start_from_flag         <- scheduler, for each detector flag (deduplicated here)

Every graph call goes through _run(), which mirrors the resulting status into
the `cases` table and isolates failures: one broken case is marked 'error'
and the rest of the cycle carries on.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Callable, Literal

from . import clock
from .detector import Flag
from .graph.build import case_state, pending_interrupt, recheck_case, resume_case, start_case
from .schemas import NormalizedStatus as S
from .schemas import TrackingEvent
from .tools import ToolContext
from .tracking_db import ACTIVE_CASE, TrackingDB

log = logging.getLogger(__name__)

# A new scan with one of these statuses re-runs an active case immediately,
# instead of waiting for its next scheduled recheck.
WAKE_ON = {S.DELIVERED.value, S.DELIVERY_FAILED.value, S.DAMAGED.value, S.RTO_INITIATED.value}
WAKEABLE_CASE = ("monitoring", "waiting_customer", "awaiting_approval")


@dataclass
class InboundResult:
    handled: bool
    shipment_id: str | None
    detail: str
    status: str | None = None


@dataclass
class StartDecision:
    action: Literal["started", "skipped", "error"]
    shipment_id: str
    reason: str
    status: str | None = None


class CaseService:
    def __init__(self, graph, ctx: ToolContext, db: TrackingDB | None = None):
        self.graph, self.ctx = graph, ctx
        self.db = db or TrackingDB(":memory:")

    # ------------------------------------------------------------------ core
    def _run(self, shipment_id: str, fn: Callable[[], dict]) -> dict | None:
        try:
            state = fn()
        except Exception as e:  # isolate: one bad case must not stop the cycle
            log.exception("Case %s failed", shipment_id)
            self.db.mark_error(shipment_id, f"{type(e).__name__}: {e}")
            return None
        self.db.sync_case(shipment_id, state)
        return state

    def start(self, shipment_id: str, carrier: str, order_id: str, trigger: str) -> dict | None:
        self.db.open_case(shipment_id, trigger)
        return self._run(shipment_id, lambda: start_case(self.graph, shipment_id, carrier, order_id, trigger))

    def recheck(self, shipment_id: str) -> dict | None:
        return self._run(shipment_id, lambda: recheck_case(self.graph, shipment_id))

    def state(self, shipment_id: str) -> dict:
        return case_state(self.graph, shipment_id)

    # ------------------------------------------------------------------ detection -> cases
    def start_from_flag(self, flag: Flag) -> StartDecision:
        """At most one active case per shipment. Existing cases are advanced by their own
        rechecks, timeouts and wake-ups, never by a second thread."""
        existing = self.db.case(flag.shipment_id)
        if existing and existing.status in ACTIVE_CASE:
            return StartDecision("skipped", flag.shipment_id, f"case already {existing.status}", existing.status)
        state = self.start(flag.shipment_id, flag.carrier, flag.order_id, flag.trigger)
        if state is None:
            return StartDecision("error", flag.shipment_id, self.db.case(flag.shipment_id).last_error)
        return StartDecision("started", flag.shipment_id, flag.rule, state.get("status"))

    def run_due_rechecks(self, now: datetime | None = None, limit: int | None = None) -> list[str]:
        due = self.db.due_rechecks(now or clock.now())[:limit]
        for awb in due:
            self.recheck(awb)
        return due

    def on_carrier_event(self, shipment_id: str, event: TrackingEvent) -> str | None:
        """Called after a webhook scan is stored. Returns the new case status if it woke a case."""
        case = self.db.case(shipment_id)
        if case and case.status in WAKEABLE_CASE and event.normalized_status.value in WAKE_ON:
            state = self.recheck(shipment_id)
            return state.get("status") if state else "error"
        return None

    # ------------------------------------------------------------------ customer replies
    def handle_inbound_message(self, from_contact: str, text: str) -> InboundResult:
        order_id = self.ctx.orders.find_by_contact(from_contact)
        if order_id is None:
            return InboundResult(False, None, "Unknown sender; route to the general support inbox.")
        awb = self.ctx.orders.get(order_id).awb
        waiting = pending_interrupt(self.graph, awb) if self.db.case(awb) else None
        if not waiting or waiting.get("kind") != "customer_reply":
            # Not something we asked about: keep it on the case and let a person see it.
            self.ctx.cases.record_reply(awb, text)
            return InboundResult(False, awb, "No open question for this customer; message stored for support.")
        state = self._run(awb, lambda: resume_case(self.graph, awb, {"type": "reply", "text": text}))
        return InboundResult(state is not None, awb, "Reply applied to case." if state else "Case failed; see last_error.",
                             state.get("status") if state else "error")

    def fire_due_timeouts(self, now: datetime | None = None) -> list[str]:
        """Resume every case whose customer reply deadline has passed. Returns the shipment ids fired."""
        now = now or clock.now()
        fired = []
        for awb in self.db.due_timeouts(now):
            waiting = pending_interrupt(self.graph, awb)
            if waiting and waiting.get("kind") == "customer_reply":
                self._run(awb, lambda awb=awb: resume_case(self.graph, awb, {"type": "timeout"}))
                fired.append(awb)
        return fired

    # ------------------------------------------------------------------ approvals
    def pending_approvals(self) -> list[dict]:
        out = []
        for row in self.db.cases("awaiting_approval"):
            waiting = pending_interrupt(self.graph, row.shipment_id)
            if waiting and waiting.get("kind") == "approval":
                out.append(waiting)
        return out

    def decide(self, shipment_id: str, approver: str, role: Literal["agent", "manager"],
               approved: bool, note: str = "") -> dict:
        waiting = pending_interrupt(self.graph, shipment_id)
        if not waiting or waiting.get("kind") != "approval":
            raise ValueError(f"{shipment_id} is not waiting for an approval.")
        state = self._run(shipment_id, lambda: resume_case(
            self.graph, shipment_id, {"approved": approved, "approver": approver, "role": role, "note": note}))
        if state is None:
            raise RuntimeError(f"Case {shipment_id} failed: {self.db.case(shipment_id).last_error}")
        return state
