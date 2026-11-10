"""The two LLM-backed decisions the graph needs, behind one small interface.

LLMBrain     -> real investigator + diagnoser from chains.py
ScriptedBrain -> deterministic, no API key; diagnoses from the seed scenarios.
                Used by tests and by `python run_graph.py --scripted` so the graph
                logic can be checked without spending tokens.
"""
from __future__ import annotations

import re
from datetime import timedelta
from typing import Protocol
from zoneinfo import ZoneInfo

from .. import clock
from ..chains import InvestigationReport
from ..scenarios import BY_AWB
from ..schemas import Address, CustomerReplyParse, DeliverySlot, Diagnosis, OrderDetails
from ..tools import ReadTools, ToolContext

IST = ZoneInfo("Asia/Kolkata")


class Brain(Protocol):
    def investigate(self, awb: str, carrier: str, order_id: str, trigger: str) -> InvestigationReport: ...

    def diagnose(self, report: InvestigationReport, awb: str, carrier: str, order_id: str,
                 trigger: str) -> Diagnosis: ...

    def parse_reply(self, text: str, question: str, order: OrderDetails) -> CustomerReplyParse: ...


class LLMBrain:
    def __init__(self, ctx: ToolContext, llm=None):
        from ..chains import get_llm
        from ..tools import build_read_tools

        self.llm = llm or get_llm()
        self.tools = build_read_tools(ctx)

    def investigate(self, awb, carrier, order_id, trigger):
        from ..chains import run_investigation

        return run_investigation(self.llm, self.tools, awb, carrier, order_id, trigger)

    def diagnose(self, report, awb, carrier, order_id, trigger):
        from ..chains import diagnose

        return diagnose(self.llm, report, awb, carrier, order_id, trigger)

    def parse_reply(self, text, question, order):
        from ..chains import parse_reply

        return parse_reply(self.llm, text, question)


_SEVERITY = {"lost": "high", "damaged": "medium", "unclear": "medium", "wrong_address": "medium",
             "failed_attempt": "medium", "delayed": "low", "no_issue": "low"}


class ScriptedBrain:
    """Returns each scenario's expected diagnosis. Overrides let tests force other answers."""

    def __init__(self, ctx: ToolContext, overrides: dict[str, dict] | None = None):
        self.read = ReadTools(ctx)
        self.overrides = overrides or {}
        self.calls: list[str] = []

    def investigate(self, awb, carrier, order_id, trigger):
        self.calls.append(f"investigate:{awb}")
        t = self.read.get_tracking_status(awb, carrier)
        notes = self.read.get_carrier_notes(awb, carrier).get("notes", [])
        findings = (f"Tracking: {t.get('current_status')} at {t.get('events', [{}])[0].get('location')}, "
                    f"last scan {t.get('hours_since_last_scan')}h ago, past EDD={t.get('is_past_edd')}. "
                    f"Carrier notes: {notes or 'none'}.")
        return InvestigationReport(findings=findings, tool_calls=[{"name": "get_tracking_status", "args": {"awb": awb}}])

    def diagnose(self, report, awb, carrier, order_id, trigger):
        self.calls.append(f"diagnose:{awb}")
        sc = BY_AWB[awb]
        status = self.read.get_tracking_status(awb, carrier).get("current_status")
        if status == "delivered" and awb not in self.overrides:
            return Diagnosis(issue_type="no_issue", severity="low", confidence=0.95,
                             evidence=["Latest scan: delivered"], recommended_action="close_resolved",
                             summary_for_human=f"{awb} has been delivered; nothing to do.")
        base = dict(
            issue_type=sc.expected_issue,
            severity=_SEVERITY[sc.expected_issue],
            confidence=0.4 if sc.expected_issue == "unclear" else 0.9,
            evidence=[report.findings],
            recommended_action=sc.expected_action,
            policy_refs=[],
            summary_for_human=f"{sc.description} ({awb}, order {order_id}).",
        )
        return Diagnosis(**{**base, **self.overrides.get(awb, {})})

    # ------------------------------------------------------------------ replies
    _WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
    _UPSET = ("worst", "useless", "angry", "complaint", "fraud", "cheat", "refund")

    def parse_reply(self, text, question, order):
        """Small rule-based parser standing in for the LLM in tests and scripted demos."""
        self.calls.append("parse_reply")
        t = text.lower()
        upset = any(w in t for w in self._UPSET)

        if "pickup" in t:
            return CustomerReplyParse(intent="pickup_point", confidence=0.9, needs_human=upset)
        if "cancel" in t:
            return CustomerReplyParse(intent="cancel", confidence=0.9, needs_human=True)

        pin = re.search(r"\b(\d{6})\b", text)
        if pin:
            line1 = text[: pin.start()].strip(" ,.-") or text
            addr = Address(line1=line1, city=order.shipping_address.city,
                           state=order.shipping_address.state, pincode=pin.group(1))
            return CustomerReplyParse(intent="new_address", address=addr, confidence=0.85, needs_human=upset)

        today = clock.now().astimezone(IST).date()
        day = None
        if "today" in t:
            day = today
        elif "tomorrow" in t:
            day = today + timedelta(days=1)
        else:
            m = re.search(r"\b(\d{4}-\d{2}-\d{2})\b", t)
            if m:
                from datetime import date
                day = date.fromisoformat(m.group(1))
            else:
                for i, name in enumerate(self._WEEKDAYS):
                    if name in t:
                        day = today + timedelta(days=(i - today.weekday()) % 7 or 7)
                        break
        if day:
            window = next((w for w in ("afternoon", "evening", "morning") if w in t), "morning")
            return CustomerReplyParse(intent="reschedule", slot=DeliverySlot(date=day.isoformat(), window=window),
                                      confidence=0.9, needs_human=upset)

        return CustomerReplyParse(intent="other", confidence=0.3, needs_human=True)
