"""Graph nodes. Each takes the CaseState and returns a partial update.

Nodes are plain methods on `Nodes`, so they can be unit-tested without
LangGraph and wired into any graph runner.

Waiting nodes (wait_for_customer, await_approval) call `self.interrupt(...)`.
In LangGraph that is `langgraph.types.interrupt`: the run pauses, the state is
checkpointed, and when the thread is resumed with Command(resume=value) the
node runs again from the top and interrupt() returns `value`. Because of that
re-run, waiting nodes do nothing before interrupt() except read state; every
side effect (sending the question, a reminder) lives in the node before it.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any, Callable

from .. import clock
from ..chains import InvestigationReport
from ..config import SETTINGS, Settings
from ..guardrails import HIGH_VALUE_THRESHOLD, apply_guardrails
from ..messaging import TEMPLATES
from ..schemas import Address, CustomerReplyParse, DeliverySlot, Diagnosis, OrderDetails, TrackingResult
from ..stores import ApprovalRequired
from ..tools import ActionTools, ReadTools, ToolContext
from .brain import IST, Brain
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

InterruptFn = Callable[[Any], Any]


def _act(result, **extra) -> dict:
    return {"action": result.action, "ok": result.ok, "reference": result.reference,
            "detail": result.detail, "at": clock.now().isoformat(timespec="seconds"), **extra}


def _fmt_date(dt: datetime) -> str:
    return dt.strftime("%a, %d %b")


def _no_interrupt(payload):
    raise RuntimeError("This runner has no interrupt support; build the graph with build_graph() or the simulator.")


class Nodes:
    def __init__(self, ctx: ToolContext, brain: Brain, settings: Settings = SETTINGS,
                 interrupt: InterruptFn = _no_interrupt):
        self.ctx, self.brain, self.settings, self.interrupt = ctx, brain, settings, interrupt
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

    def _deadline(self) -> str:
        return (clock.now() + timedelta(hours=self.settings.reply_timeout_hours)).isoformat(timespec="minutes")

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
            # Clear leftovers from a previous run of this thread.
            "escalation_reason": None, "approval_error": None, "customer_event": None, "parsed_reply": None,
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

    # ------------------------------------------------------------------ automatic actions
    def notify_delay(self, s: CaseState) -> dict:
        t = self._tracking(s)
        base = t.expected_delivery_date if t else clock.now()
        new_eta = max(base, clock.now()) + timedelta(days=self.settings.delay_eta_buffer_days)
        # One delay message per revised ETA (policy: at most one unless the new date also passes).
        r = self._message(s, "delay_update", {"new_eta": _fmt_date(new_eta)},
                          dedupe_key=f"delay_update:{new_eta.date()}")
        return {"actions": [_act(r, new_eta=new_eta.date().isoformat())], "log": [f"notify_delay: {r.detail}"]}

    def carrier_inquiry(self, s: CaseState) -> dict:
        d = self._diag(s)
        inq = self.tools.raise_carrier_inquiry(s["shipment_id"], s["carrier"], d.summary_for_human)
        # Policy: tell the customer we're investigating; never say "lost" yet.
        msg = self._message(s, "investigating", {"hours": self.settings.inquiry_recheck_hours})
        return {"actions": [_act(inq), _act(msg)],
                "log": [f"carrier_inquiry: {inq.reference}; customer: {msg.detail}"]}

    # ------------------------------------------------------------------ asking the customer
    def _ask(self, s: CaseState, kind: str, template_id: str) -> dict:
        r = self._message(s, template_id, {})
        o = self._order(s)
        question = TEMPLATES[template_id].format(first_name=o.customer_first_name, order_id=o.order_id)
        return {"actions": [_act(r)], "status": "waiting_customer", "awaiting": kind, "question": question,
                "reply_deadline": self._deadline(), "reminders_sent": 0,
                "log": [f"ask_{kind}: {r.detail}"]}

    def ask_reschedule(self, s: CaseState) -> dict:
        return self._ask(s, "reschedule", "reschedule_request")

    def ask_address(self, s: CaseState) -> dict:
        return self._ask(s, "address", "address_confirm")

    def wait_for_customer(self, s: CaseState) -> dict:
        # Pauses here. Resume with {"type": "reply", "text": "..."} or {"type": "timeout"}.
        event = self.interrupt({
            "kind": "customer_reply",
            "shipment_id": s["shipment_id"],
            "awaiting": s.get("awaiting"),
            "reply_deadline": s.get("reply_deadline"),
            "reminders_sent": s.get("reminders_sent", 0),
        })
        if not isinstance(event, dict) or event.get("type") not in ("reply", "timeout"):
            event = {"type": "reply", "text": str(event)}
        if event["type"] == "reply":
            self.ctx.cases.record_reply(s["shipment_id"], event.get("text", ""))
        return {"customer_event": event, "log": [f"wait_for_customer: {event['type']}"]}

    def send_reminder(self, s: CaseState) -> dict:
        n = s.get("reminders_sent", 0) + 1
        r = self._message(s, "reply_reminder", {}, dedupe_key=f"reply_reminder:{n}")
        return {"actions": [_act(r)], "reminders_sent": n, "reply_deadline": self._deadline(),
                "log": [f"send_reminder #{n}: {r.detail}"]}

    def handle_no_reply(self, s: CaseState) -> dict:
        # Policy: after the reminder window, let the carrier's own reattempt run its course,
        # but COD orders go to a person (they are the ones most likely to end up RTO).
        o = self._order(s)
        if o.payment_mode == "cod":
            return {"escalation_reason": "No customer reply after reminder on a COD order (RTO risk).",
                    "log": ["handle_no_reply: COD -> escalate"]}
        return {"log": ["handle_no_reply: prepaid -> rely on carrier reattempt, recheck later"]}

    def parse_reply(self, s: CaseState) -> dict:
        event = s["customer_event"]
        parsed = event.get("parsed")
        if parsed is not None:
            p = CustomerReplyParse(**parsed)  # pre-parsed (e.g. a WhatsApp button or a test)
        else:
            p = self.brain.parse_reply(event.get("text", ""), s.get("question", ""), self._order(s))
        reason = self._reply_problem(s, p)
        return {"parsed_reply": p.model_dump(mode="json"), "escalation_reason": reason,
                "log": [f"parse_reply: {p.intent} ({p.confidence:.2f})" + (f" -> needs human: {reason}" if reason else "")]}

    def _reply_problem(self, s: CaseState, p: CustomerReplyParse) -> str | None:
        """Why this reply can't be handled automatically, or None if it can."""
        if p.needs_human:
            return "Customer reply needs a person (upset, ambiguous or a question)."
        if p.confidence < self.settings.min_reply_confidence:
            return f"Low confidence reading the reply ({p.confidence:.2f})."
        awaiting = s.get("awaiting")
        if p.intent == "reschedule":
            if not p.slot:
                return "Reply mentions rescheduling but gives no usable day."
            today = clock.now().astimezone(IST).date()
            day = date.fromisoformat(p.slot.date)
            if day < today or day > today + timedelta(days=self.settings.max_slot_days_ahead):
                return f"Requested day {p.slot.date} is outside the allowed window."
            return None
        if p.intent == "new_address":
            return None if p.address else "Reply looks like an address but is incomplete."
        if p.intent == "pickup_point":
            return "Customer wants to collect from a pickup point (not automated yet)."
        if p.intent == "cancel":
            return "Customer wants to cancel the order."
        return f"Reply doesn't answer the {awaiting or 'question'}."

    def rebook(self, s: CaseState) -> dict:
        slot = DeliverySlot(**s["parsed_reply"]["slot"])
        r = self.tools.request_rebooking(s["shipment_id"], s["carrier"], slot)
        update = {"actions": [_act(r)], "last_action_ok": r.ok, "log": [f"rebook: {r.detail}"]}
        if r.ok:
            msg = self._message(s, "reattempt_confirmed", {"date": _fmt_date(datetime.fromisoformat(slot.date)),
                                                           "window": slot.window},
                                dedupe_key=f"reattempt_confirmed:{slot.date}")
            update["actions"].append(_act(msg))
            update["awaiting"] = None
        else:
            update["escalation_reason"] = f"Rebooking failed: {r.detail}"
        return update

    def update_address(self, s: CaseState) -> dict:
        addr = Address(**s["parsed_reply"]["address"])
        r = self.tools.update_delivery_address(s["shipment_id"], s["carrier"], addr)
        update = {"actions": [_act(r)], "last_action_ok": r.ok, "log": [f"update_address: {r.detail}"]}
        if r.ok:
            update["actions"].append(_act(self._message(s, "address_updated", {})))
            update["awaiting"] = None
        else:
            update["escalation_reason"] = (f"Customer gave a corrected address but the carrier refused: {r.detail} "
                                           "Needs RTO and reshipment to the new address.")
        return update

    # ------------------------------------------------------------------ approvals
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
        return {"proposal": proposal, "status": "awaiting_approval", "approval": None, "approval_error": None,
                "log": [f"propose_resolution: {kind} ₹{amount:,.0f}, needs {proposal['approver_level']} approval"]}

    def await_approval(self, s: CaseState) -> dict:
        # Pauses here. Resume with {"approved": bool, "approver": str, "role": "agent"|"manager", "note": str}.
        decision = self.interrupt({
            "kind": "approval",
            "shipment_id": s["shipment_id"],
            "proposal": s["proposal"],
            "diagnosis": s["diagnosis"],
            "previous_error": s.get("approval_error"),
        })
        decision = decision if isinstance(decision, dict) else {}
        p = s["proposal"]
        error = None
        if not decision.get("approver"):
            error = "Decision must name the approver."
        elif decision.get("approved") and p["approver_level"] == "manager" and decision.get("role") != "manager":
            error = f"₹{p['amount']:,.0f} needs a manager's approval; {decision['approver']} is a {decision.get('role', 'agent')}."
        update = {"approval": decision, "approval_error": error,
                  "log": [f"await_approval: {'invalid: ' + error if error else ('approved' if decision.get('approved') else 'rejected')} "
                          f"by {decision.get('approver')}"]}
        if not error and not decision.get("approved"):
            update["escalation_reason"] = f"{p['kind'].title()} rejected by {decision['approver']}: {decision.get('note', '')}".strip()
        return update

    def execute_resolution(self, s: CaseState) -> dict:
        p, a, o = s["proposal"], s["approval"], self._order(s)
        try:
            if p["kind"] == "refund":
                r = self.tools.issue_refund(s["shipment_id"], p["order_id"], p["amount"], p["reason"], a["approver"])
                msg_args = ("refund_initiated", {"amount": f"{p['amount']:,.0f}"})
            else:
                r = self.tools.create_reshipment(s["shipment_id"], p["order_id"], p["skus"], a["approver"])
                msg_args = ("replacement_shipped", {})
        except ApprovalRequired as e:  # defensive: should be impossible after await_approval
            return {"last_action_ok": False, "escalation_reason": str(e), "log": [f"execute_resolution: {e}"]}
        update = {"actions": [_act(r, approved_by=a["approver"])], "last_action_ok": r.ok,
                  "log": [f"execute_resolution: {r.detail}"]}
        if r.ok:
            update["actions"].append(_act(self._message(s, *msg_args)))
        else:
            update["escalation_reason"] = f"{p['kind'].title()} failed after approval: {r.detail}"
        return update

    # ------------------------------------------------------------------ endings
    def escalate(self, s: CaseState) -> dict:
        d, o = self._diag(s), self._order(s)
        high = d.severity == "high" or o.order_value > HIGH_VALUE_THRESHOLD
        reason = s.get("escalation_reason")
        summary = "\n".join([
            *([f"Reason: {reason}"] if reason else []),
            d.summary_for_human,
            f"Order {o.order_id}: ₹{o.order_value:,.0f} ({o.payment_mode}), carrier {s['carrier']}.",
            "Evidence: " + "; ".join(d.evidence),
            *([f"Guardrails: {'; '.join(s['guardrail_overrides'])}"] if s.get("guardrail_overrides") else []),
            *([f"Customer said: {s['customer_event']['text']!r}"]
              if (s.get("customer_event") or {}).get("type") == "reply" else []),
            f"Rechecks so far: {s.get('recheck_count', 0)}",
        ])
        r = self.tools.escalate_to_support(s["shipment_id"], summary, "high" if high else "medium")
        return {"actions": [_act(r)], "status": "escalated", "next_check_at": None, "awaiting": None,
                "reply_deadline": None, "log": [f"escalate: {r.reference}"]}

    def schedule_recheck(self, s: CaseState) -> dict:
        d = self._diag(s)
        hours = (self.settings.inquiry_recheck_hours if d.recommended_action == "carrier_inquiry"
                 else self.settings.delay_recheck_hours)
        at = clock.now() + timedelta(hours=hours)
        return {"status": "monitoring", "recheck_count": s.get("recheck_count", 0) + 1,
                "next_check_at": at.isoformat(timespec="minutes"), "reply_deadline": None,
                "log": [f"schedule_recheck: in {hours}h"]}

    def close_case(self, s: CaseState) -> dict:
        self.ctx.cases.record_action(s["shipment_id"], "closed")
        return {"status": "closed", "next_check_at": None, "reply_deadline": None, "awaiting": None,
                "log": ["close_case"]}

    # ------------------------------------------------------------------ routers
    @staticmethod
    def route_after_diagnosis(s: CaseState) -> str:
        return ACTION_TO_NODE[s["diagnosis"]["recommended_action"]]

    def route_after_wait(self, s: CaseState) -> str:
        if s["customer_event"]["type"] == "reply":
            return "parse_reply"
        if s.get("reminders_sent", 0) < self.settings.max_reminders:
            return "send_reminder"
        return "handle_no_reply"

    @staticmethod
    def route_after_no_reply(s: CaseState) -> str:
        return "escalate" if s.get("escalation_reason") else "schedule_recheck"

    @staticmethod
    def route_after_parse(s: CaseState) -> str:
        if s.get("escalation_reason"):
            return "escalate"
        return {"reschedule": "rebook", "new_address": "update_address"}[s["parsed_reply"]["intent"]]

    @staticmethod
    def route_after_customer_action(s: CaseState) -> str:
        return "schedule_recheck" if s.get("last_action_ok") else "escalate"

    @staticmethod
    def route_after_approval(s: CaseState) -> str:
        if s.get("approval_error"):
            return "await_approval"  # ask again; the payload carries previous_error
        return "execute_resolution" if s["approval"].get("approved") else "escalate"

    @staticmethod
    def route_after_execution(s: CaseState) -> str:
        return "close_case" if s.get("last_action_ok") else "escalate"
