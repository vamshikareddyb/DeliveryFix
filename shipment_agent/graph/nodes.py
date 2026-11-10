"""Graph nodes. Each takes the CaseState and returns a partial update.

Nodes are plain methods on `Nodes`, so they can be unit-tested without
LangGraph and wired into any graph runner.
"""
from __future__ import annotations

from datetime import datetime, timedelta

from .. import clock
from ..chains import InvestigationReport
from ..config import SETTINGS, Settings
from ..guardrails import HIGH_VALUE_THRESHOLD, apply_guardrails
from ..schemas import Diagnosis, OrderDetails, TrackingResult
from ..tools import ActionTools, ReadTools, ToolContext
from .brain import Brain
from .state import CaseState

# Which node handles each recommended_action. The router reads this.
ACTION_TO_NODE = {
    "notify_delay": "notify_delay",
    "ask_reschedule": "ask_reschedule",
    "ask_address": "ask_address",
    "carrier_inquiry": "carrier_inquiry",
    "propose_refund": "propose_resolution",
    "propose_reship": "propose_resolution",
    "escalate": "escalate",
    "close_resolved": "close_case",
}


def _act(result, **extra) -> dict:
    return {"action": result.action, "ok": result.ok, "reference": result.reference,
            "detail": result.detail, "at": clock.now().isoformat(timespec="seconds"), **extra}


def _fmt_date(dt: datetime) -> str:
    return dt.strftime("%a, %d %b")


class Nodes:
    def __init__(self, ctx: ToolContext, brain: Brain, settings: Settings = SETTINGS):
        self.ctx, self.brain, self.settings = ctx, brain, settings
        self.read, self.tools = ReadTools(ctx), ActionTools(ctx)

    # ------------------------------------------------------------------ helpers
    def _order(self, s: CaseState) -> OrderDetails:
        return OrderDetails(**s["order"])

    def _tracking(self, s: CaseState) -> TrackingResult | None:
        return TrackingResult(**s["tracking"]) if s.get("tracking") else None

    def _diag(self, s: CaseState) -> Diagnosis:
        return Diagnosis(**s["diagnosis"])

    def _message(self, s: CaseState, template_id: str, variables: dict, dedupe_key: str | None = None):
        o = self._order(s)
        return self.tools.send_customer_message(
            s["shipment_id"], o.customer_ref, o.preferred_channel, template_id,
            {"first_name": o.customer_first_name, "order_id": o.order_id, **variables}, dedupe_key)

    # ------------------------------------------------------------------ investigate / diagnose
    def investigate(self, s: CaseState) -> dict:
        awb, carrier, order_id = s["shipment_id"], s["carrier"], s["order_id"]
        report = self.brain.investigate(awb, carrier, order_id, s.get("trigger", ""))
        # Deterministic snapshots for guardrails, independent of what the LLM chose to look at.
        tracking = self.read.get_tracking_status(awb, carrier, refresh=True)
        order = self.read.get_order_details(order_id)
        update = {
            "findings": report.findings,
            "tool_calls": report.tool_calls,
            "order": order,
            "status": "open",
            "log": [f"investigate ({s.get('trigger', '')}): {len(report.tool_calls)} tool calls"],
        }
        if "error" not in tracking:
            update["tracking"] = tracking
        else:
            update["log"].append(f"tracking snapshot failed: {tracking['error']}")
        return update

    def diagnose(self, s: CaseState) -> dict:
        d = self.brain.diagnose(
            InvestigationReport(s["findings"], s.get("tool_calls", [])),
            s["shipment_id"], s["carrier"], s["order_id"], s.get("trigger", ""),
        )
        history = self.ctx.cases.get(s["shipment_id"])
        final, overrides = apply_guardrails(d, self._order(s), self._tracking(s), history, self.settings)

        # Rechecks that keep failing get a human.
        if (s.get("recheck_count", 0) >= self.settings.max_rechecks
                and final.recommended_action in ("notify_delay", "carrier_inquiry")):
            overrides.append(f"{final.recommended_action} -> escalate: {s['recheck_count']} rechecks without resolution")
            final.recommended_action = "escalate"

        self.ctx.cases.record_diagnosis(s["shipment_id"], final.model_dump())
        return {
            "llm_diagnosis": d.model_dump(),
            "diagnosis": final.model_dump(),
            "guardrail_overrides": overrides,
            "log": [f"diagnose: {final.issue_type} ({final.confidence:.2f}) -> {final.recommended_action}"
                    + (f" [overrides: {'; '.join(overrides)}]" if overrides else "")],
        }

    # ------------------------------------------------------------------ action nodes
    def notify_delay(self, s: CaseState) -> dict:
        t = self._tracking(s)
        base = t.expected_delivery_date if t else clock.now()
        new_eta = max(base, clock.now()) + timedelta(days=self.settings.delay_eta_buffer_days)
        # One delay message per revised ETA (policy: at most one unless the new date also passes).
        r = self._message(s, "delay_update", {"new_eta": _fmt_date(new_eta)},
                          dedupe_key=f"delay_update:{new_eta.date()}")
        return {"actions": [_act(r, new_eta=new_eta.date().isoformat())], "log": [f"notify_delay: {r.detail}"]}

    def ask_reschedule(self, s: CaseState) -> dict:
        r = self._message(s, "reschedule_request", {})
        return {"actions": [_act(r)], "status": "waiting_customer", "log": [f"ask_reschedule: {r.detail}"]}

    def ask_address(self, s: CaseState) -> dict:
        r = self._message(s, "address_confirm", {})
        return {"actions": [_act(r)], "status": "waiting_customer", "log": [f"ask_address: {r.detail}"]}

    def carrier_inquiry(self, s: CaseState) -> dict:
        d = self._diag(s)
        inq = self.tools.raise_carrier_inquiry(s["shipment_id"], s["carrier"], d.summary_for_human)
        # Policy: tell the customer we're investigating; never say "lost" yet.
        msg = self._message(s, "investigating", {"hours": self.settings.inquiry_recheck_hours})
        return {"actions": [_act(inq), _act(msg)],
                "log": [f"carrier_inquiry: {inq.reference}; customer: {msg.detail}"]}

    def propose_resolution(self, s: CaseState) -> dict:
        d, o = self._diag(s), self._order(s)
        kind = "refund" if d.recommended_action == "propose_refund" else "reship"
        amount = o.order_value
        proposal = {
            "kind": kind,
            "order_id": o.order_id,
            "amount": amount,
            "skus": [i.sku for i in o.items],
            "approver_level": "manager" if amount > self.settings.refund_manager_limit else "agent",
            "reason": d.summary_for_human,
            "evidence": d.evidence,
            "policy_refs": d.policy_refs,
            "proposed_at": clock.now().isoformat(timespec="seconds"),
        }
        # Nothing is executed here. Phase 3 adds interrupt() for approval, then executes.
        return {"proposal": proposal, "status": "awaiting_approval",
                "log": [f"propose_resolution: {kind} ₹{amount:,.0f}, needs {proposal['approver_level']} approval"]}

    def escalate(self, s: CaseState) -> dict:
        d, o = self._diag(s), self._order(s)
        high = d.severity == "high" or o.order_value > HIGH_VALUE_THRESHOLD
        summary = "\n".join([
            d.summary_for_human,
            f"Order {o.order_id}: ₹{o.order_value:,.0f} ({o.payment_mode}), carrier {s['carrier']}.",
            "Evidence: " + "; ".join(d.evidence),
            *([f"Guardrails: {'; '.join(s['guardrail_overrides'])}"] if s.get("guardrail_overrides") else []),
            f"Rechecks so far: {s.get('recheck_count', 0)}",
        ])
        r = self.tools.escalate_to_support(s["shipment_id"], summary, "high" if high else "medium")
        return {"actions": [_act(r)], "status": "escalated", "next_check_at": None,
                "log": [f"escalate: {r.reference}"]}

    def schedule_recheck(self, s: CaseState) -> dict:
        d = self._diag(s)
        hours = (self.settings.inquiry_recheck_hours if d.recommended_action == "carrier_inquiry"
                 else self.settings.delay_recheck_hours)
        at = clock.now() + timedelta(hours=hours)
        return {"status": "monitoring", "recheck_count": s.get("recheck_count", 0) + 1,
                "next_check_at": at.isoformat(timespec="minutes"),
                "log": [f"schedule_recheck: in {hours}h"]}

    def close_case(self, s: CaseState) -> dict:
        self.ctx.cases.record_action(s["shipment_id"], "closed")
        return {"status": "closed", "next_check_at": None, "log": ["close_case"]}

    # ------------------------------------------------------------------ routing
    @staticmethod
    def route_after_diagnosis(s: CaseState) -> str:
        return ACTION_TO_NODE[s["diagnosis"]["recommended_action"]]

