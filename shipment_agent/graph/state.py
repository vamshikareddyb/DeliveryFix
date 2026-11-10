"""Graph state for one shipment case (one LangGraph thread per shipment).

Fields are JSON-friendly (dicts, not Pydantic objects) so any checkpointer
can persist them. `actions` and `log` are append-only via the add reducer.
"""
from __future__ import annotations

import operator
from typing import Annotated, Literal, Optional, TypedDict

CaseStatus = Literal[
    "open",               # being worked on in this run
    "monitoring",         # waiting for the next recheck (delay notice sent / inquiry raised)
    "waiting_customer",   # asked the customer something; Phase 3 resumes on reply
    "awaiting_approval",  # refund/reship proposed; Phase 3 resumes on approval
    "escalated",          # handed to a human
    "closed",
]


class CaseState(TypedDict, total=False):
    # Identity (set on first run, stays fixed)
    shipment_id: str
    carrier: str
    order_id: str

    # Why this run started: "stale scan 60h", "delivery failed", "recheck", ...
    trigger: str

    # Investigation output
    findings: str
    tool_calls: list[dict]
    tracking: dict  # TrackingResult snapshot used by guardrails
    order: dict     # OrderDetails snapshot

    # Diagnosis output
    diagnosis: dict
    llm_diagnosis: dict          # before guardrails, kept for evaluation
    guardrail_overrides: list[str]

    # Waiting on the customer (Phase 3)
    awaiting: Optional[str]          # "reschedule" | "address": what we asked for
    question: Optional[str]          # the message we sent, given to the reply parser
    reply_deadline: Optional[str]    # ISO time; the scheduler fires a timeout after this
    reminders_sent: int
    customer_event: Optional[dict]   # {"type": "reply", "text": ...} or {"type": "timeout"}
    parsed_reply: Optional[dict]     # CustomerReplyParse
    no_reply_attempt: Optional[int]  # delivery attempt the customer never answered about

    # Waiting on staff (Phase 3)
    approval: Optional[dict]         # {"approved", "approver", "role", "note"}
    approval_error: Optional[str]

    escalation_reason: Optional[str]
    last_action_ok: Optional[bool]

    # Resolution
    proposal: Optional[dict]     # refund / reship awaiting approval
    status: CaseStatus
    recheck_count: int
    next_check_at: Optional[str]  # ISO time; the Phase 4 scheduler reads this

    # Append-only history
    actions: Annotated[list[dict], operator.add]
    log: Annotated[list[str], operator.add]


# Keys whose updates append instead of overwrite (used by the local simulator too).
APPEND_KEYS = {"actions", "log"}
