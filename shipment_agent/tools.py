"""The tools, split by who is allowed to call them.

READ tools  -> given to the LLM during investigation (build_read_tools).
ACTION tools -> called only by graph nodes after routing (ActionTools).
                They are deliberately NOT wrapped as LangChain tools, so the
                model can never trigger a message, rebooking or refund itself.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from pydantic import BaseModel, Field

from .carriers import CarrierAPIError
from .carriers.mock import default_carriers
from .messaging import Messenger, Outbox
from .policy import PolicyRetriever
from .schemas import ActionResult, Address, DeliverySlot
from .stores import BackOffice, CaseStore, OrderStore

MAX_EVENTS_FOR_LLM = 10


@dataclass
class ToolContext:
    """Everything the tools need. Built once per process; shared across cases."""

    carriers: dict = field(default_factory=default_carriers)
    orders: OrderStore = field(default_factory=OrderStore)
    cases: CaseStore = field(default_factory=CaseStore)
    backoffice: BackOffice = field(default_factory=BackOffice)
    policy: PolicyRetriever = field(default_factory=PolicyRetriever)
    outbox: Outbox = field(default_factory=Outbox)

    def __post_init__(self):
        self.messenger = Messenger(self.orders, self.cases, self.outbox)


def _carrier_error(e: Exception) -> dict:
    return {"error": str(e), "retryable": isinstance(e, CarrierAPIError)}


# --------------------------------------------------------------------------
# READ tools (plain functions; the LangChain wrappers are built below)
# --------------------------------------------------------------------------
class ReadTools:
    def __init__(self, ctx: ToolContext):
        self.ctx = ctx

    def get_tracking_status(self, awb: str, carrier: str, refresh: bool = False) -> dict:
        try:
            t = self.ctx.carriers[carrier].get_tracking(awb, refresh=refresh)
        except (CarrierAPIError, KeyError) as e:
            return _carrier_error(e)
        data = t.model_dump(mode="json")
        data["events"] = data["events"][:MAX_EVENTS_FOR_LLM]
        return data

    def get_carrier_notes(self, awb: str, carrier: str) -> dict:
        try:
            return self.ctx.carriers[carrier].get_notes(awb).model_dump(mode="json")
        except (CarrierAPIError, KeyError) as e:
            return _carrier_error(e)

    def get_order_details(self, order_id: str) -> dict:
        try:
            return self.ctx.orders.get(order_id).model_dump(mode="json")
        except KeyError:
            return {"error": f"Order {order_id} not found"}

    def get_case_history(self, shipment_id: str) -> dict:
        return self.ctx.cases.get(shipment_id).model_dump(mode="json")

    def search_policy(self, query: str, carrier: Optional[str] = None, topic: Optional[str] = None) -> dict:
        hits = self.ctx.policy.search(query, carrier=carrier, topic=topic, k=3)
        return {"results": [h.model_dump() for h in hits]}


# Argument schemas give the LLM clear descriptions for each parameter.
class _TrackingArgs(BaseModel):
    awb: str = Field(description="Air waybill (tracking) number.")
    carrier: str = Field(description="Carrier slug, e.g. 'delhivery' or 'bluedart'.")
    refresh: bool = Field(False, description="Fetch live from the carrier instead of the cached copy. Use before concluding a parcel is lost.")


class _NotesArgs(BaseModel):
    awb: str
    carrier: str


class _OrderArgs(BaseModel):
    order_id: str


class _CaseArgs(BaseModel):
    shipment_id: str = Field(description="Same as the AWB number.")


class _PolicyArgs(BaseModel):
    query: str = Field(description="What you want to know, e.g. 'can address be changed in transit'.")
    carrier: Optional[str] = Field(None, description="Limit to policies for this carrier (plus general ones).")
    topic: Optional[str] = Field(
        None, description="Optional: delay, lost, damaged, refund, failed_attempt, carrier_capability, escalation."
    )


READ_TOOL_SPECS = [
    ("get_tracking_status", _TrackingArgs,
     "Latest tracking status and scan history for a shipment, normalized across carriers. "
     "Includes hours since last scan, expected delivery date and whether it is past EDD."),
    ("get_carrier_notes", _NotesArgs,
     "Free-text remarks the carrier recorded on scans, e.g. 'address incomplete' or 'customer not available'."),
    ("get_order_details", _OrderArgs,
     "Order value, items, payment mode (prepaid/COD), shipping address and preferred contact channel. "
     "Contact details are masked."),
    ("get_case_history", _CaseArgs,
     "Messages already sent to the customer, their replies, earlier diagnoses and actions taken for this shipment."),
    ("search_policy", _PolicyArgs,
     "Search company policy: refund rules, delay communication, carrier capabilities, escalation rules. "
     "Cite the returned chunk_id in your diagnosis."),
]


def build_read_tools(ctx: ToolContext) -> list:
    """Wrap the read functions as LangChain StructuredTools for bind_tools()."""
    from langchain_core.tools import StructuredTool  # imported lazily

    impl = ReadTools(ctx)
    return [
        StructuredTool.from_function(func=getattr(impl, name), name=name, description=desc, args_schema=schema)
        for name, schema, desc in READ_TOOL_SPECS
    ]


# --------------------------------------------------------------------------
# ACTION tools (graph nodes only)
# --------------------------------------------------------------------------
class ActionTools:
    def __init__(self, ctx: ToolContext):
        self.ctx = ctx

    def send_customer_message(self, shipment_id: str, customer_ref: str, channel: str,
                              template_id: str, variables: dict, dedupe_key: str | None = None) -> ActionResult:
        return self.ctx.messenger.send(shipment_id, customer_ref, channel, template_id, variables, dedupe_key)

    def request_rebooking(self, shipment_id: str, carrier: str, slot: DeliverySlot) -> ActionResult:
        r = self.ctx.carriers[carrier].request_reattempt(shipment_id, slot)
        if r.ok:
            self.ctx.cases.record_action(shipment_id, f"rebooked:{slot.date}:{slot.window}")
        return r

    def update_delivery_address(self, shipment_id: str, carrier: str, address: Address) -> ActionResult:
        r = self.ctx.carriers[carrier].update_address(shipment_id, address)
        if r.ok:
            order_id = self.ctx.orders.order_id_for_awb(shipment_id)
            self.ctx.orders.update_address(order_id, address)
            self.ctx.cases.record_action(shipment_id, "address_updated")
        return r

    def raise_carrier_inquiry(self, shipment_id: str, carrier: str, issue: str) -> ActionResult:
        r = self.ctx.carriers[carrier].raise_inquiry(shipment_id, issue)
        if r.ok:
            self.ctx.cases.record_action(shipment_id, f"inquiry:{r.reference}")
        return r

    def issue_refund(self, shipment_id: str, order_id: str, amount: float, reason: str,
                     approved_by: str | None) -> ActionResult:
        r = self.ctx.backoffice.issue_refund(order_id, amount, reason, approved_by)
        if r.ok:
            self.ctx.cases.record_action(shipment_id, f"refund:{r.reference}")
        return r

    def create_reshipment(self, shipment_id: str, order_id: str, skus: list[str],
                          approved_by: str | None) -> ActionResult:
        r = self.ctx.backoffice.create_reshipment(order_id, skus, approved_by)
        if r.ok:
            self.ctx.cases.record_action(shipment_id, f"reship:{r.reference}")
        return r

    def escalate_to_support(self, shipment_id: str, summary: str, priority: str = "medium") -> ActionResult:
        r = self.ctx.backoffice.escalate(shipment_id, summary, priority)
        self.ctx.cases.record_action(shipment_id, f"escalated:{r.reference}")
        return r
