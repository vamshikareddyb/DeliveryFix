"""Shared data models.

Everything the tools return and everything the LLM produces is defined here,
so the rest of the code (and later the LangGraph state) has one source of truth.
"""
from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Literal, Optional

from pydantic import BaseModel, Field


# --------------------------------------------------------------------------
# Tracking
# --------------------------------------------------------------------------
class NormalizedStatus(str, Enum):
    PICKED_UP = "picked_up"
    IN_TRANSIT = "in_transit"
    OUT_FOR_DELIVERY = "out_for_delivery"
    DELIVERY_FAILED = "delivery_failed"
    DELIVERED = "delivered"
    RTO_INITIATED = "rto_initiated"  # return to origin
    CUSTOMS_HOLD = "customs_hold"
    DAMAGED = "damaged"
    UNKNOWN = "unknown"


class TrackingEvent(BaseModel):
    raw_status: str
    normalized_status: NormalizedStatus
    location: str
    remarks: Optional[str] = None
    event_time: datetime


class TrackingResult(BaseModel):
    awb: str
    carrier: str
    current_status: NormalizedStatus
    last_scan_at: datetime
    hours_since_last_scan: float
    expected_delivery_date: datetime
    is_past_edd: bool
    attempt_count: int
    events: list[TrackingEvent]  # newest first


class CarrierNotes(BaseModel):
    awb: str
    carrier: str
    notes: list[str]  # remarks from failed attempts, exceptions, etc.


# --------------------------------------------------------------------------
# Orders and cases
# --------------------------------------------------------------------------
class Address(BaseModel):
    line1: str
    line2: Optional[str] = None
    city: str
    state: str
    pincode: str = Field(pattern=r"^\d{6}$")


class OrderItem(BaseModel):
    sku: str
    name: str
    qty: int
    unit_price: float


class OrderDetails(BaseModel):
    """What the LLM sees. Contact details are masked; actions use customer_ref."""

    order_id: str
    awb: str
    carrier: str
    customer_ref: str
    customer_first_name: str
    preferred_channel: Literal["whatsapp", "sms", "email"]
    masked_phone: str
    shipping_address: Address
    items: list[OrderItem]
    order_value: float
    payment_mode: Literal["prepaid", "cod"]


class SentMessage(BaseModel):
    template_id: str
    channel: str
    sent_at: datetime
    rendered: str


class CaseHistory(BaseModel):
    shipment_id: str
    messages_sent: list[SentMessage] = []
    customer_replies: list[str] = []
    previous_diagnoses: list[dict] = []
    actions_taken: list[str] = []


# --------------------------------------------------------------------------
# Policy (RAG)
# --------------------------------------------------------------------------
class PolicyChunk(BaseModel):
    chunk_id: str
    title: str
    text: str
    metadata: dict[str, str] = {}
    score: float = 0.0


# --------------------------------------------------------------------------
# Action inputs and results
# --------------------------------------------------------------------------
class DeliverySlot(BaseModel):
    date: str  # YYYY-MM-DD
    window: Literal["morning", "afternoon", "evening"]


class ActionResult(BaseModel):
    ok: bool
    action: str
    reference: Optional[str] = None
    detail: str = ""


# --------------------------------------------------------------------------
# LLM structured outputs
# --------------------------------------------------------------------------
IssueType = Literal["delayed", "failed_attempt", "wrong_address", "lost", "damaged", "unclear"]
RecommendedAction = Literal[
    "notify_delay",
    "ask_reschedule",
    "ask_address",
    "carrier_inquiry",
    "propose_refund",
    "propose_reship",
    "escalate",
    "close_resolved",
]


class Diagnosis(BaseModel):
    """Classification of one shipment exception."""

    issue_type: IssueType = Field(description="What went wrong. Use 'unclear' rather than guessing.")
    severity: Literal["low", "medium", "high"]
    confidence: float = Field(ge=0, le=1, description="How sure you are of issue_type, 0 to 1.")
    evidence: list[str] = Field(
        min_length=1, description="Specific facts from the tool results that support this diagnosis."
    )
    recommended_action: RecommendedAction
    policy_refs: list[str] = Field(default_factory=list, description="chunk_ids of policies relied on.")
    summary_for_human: str = Field(description="2-3 sentences a support agent could act on.")


class CustomerReplyParse(BaseModel):
    """Structured reading of a customer's reply to our message."""

    intent: Literal["reschedule", "new_address", "pickup_point", "cancel", "other"]
    slot: Optional[DeliverySlot] = None
    address: Optional[Address] = None
    confidence: float = Field(ge=0, le=1)
    needs_human: bool = Field(description="True if the reply is angry, ambiguous or off-topic.")
