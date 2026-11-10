"""Graph state for one shipment case (one LangGraph thread per shipment)."""
from __future__ import annotations

import operator
from typing import Annotated, Literal, TypedDict

CaseStatus = Literal[
    "open",
    "monitoring",          # delay notice sent; recheck later (loop added in Phase 3)
    "awaiting_customer",   # asked for a new slot or address
    "awaiting_carrier",    # trace request raised for a suspected loss
    "awaiting_approval",   # refund / reship proposal waiting for a human
    "escalated",
    "closed",
]


class CaseState(TypedDict, total=False):
    # Input (set by the detector when it starts the case)
    awb: str
    carrier: str
    order_id: str
    trigger: str

    # Context loaded deterministically, so guardrails never depend on the LLM's tool use
    tracking: dict
    order: dict

    # LLM outputs
    findings: str
    tool_calls: list[dict]
    diagnosis: dict
    overrides: list[str]

    # Results
    proposal: dict  # refund / reship awaiting approval
    actions: Annotated[list[str], operator.add]  # appended to by every action node
    status: CaseStatus
