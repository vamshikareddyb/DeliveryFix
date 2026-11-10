"""The two LLM-backed decisions the graph needs, behind one small interface.

LLMBrain     -> real investigator + diagnoser from chains.py
ScriptedBrain -> deterministic, no API key; diagnoses from the seed scenarios.
                Used by tests and by `python run_graph.py --scripted` so the graph
                logic can be checked without spending tokens.
"""
from __future__ import annotations

from typing import Protocol

from ..chains import InvestigationReport
from ..scenarios import BY_AWB
from ..schemas import Diagnosis
from ..tools import ReadTools, ToolContext


class Brain(Protocol):
    def investigate(self, awb: str, carrier: str, order_id: str, trigger: str) -> InvestigationReport: ...

    def diagnose(self, report: InvestigationReport, awb: str, carrier: str, order_id: str,
                 trigger: str) -> Diagnosis: ...


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
