"""Customer messaging: approved templates only, idempotent per case.

The LLM never writes free text to customers. Nodes pick a template_id and
fill variables; anything missing raises before a message goes out.
"""
from __future__ import annotations

import string
from dataclasses import dataclass, field

from . import clock
from .schemas import ActionResult, SentMessage
from .stores import CaseStore, OrderStore

TEMPLATES: dict[str, str] = {
    "delay_update": (
        "Hi {first_name}, your order {order_id} is running a little late. "
        "The new expected delivery date is {new_eta}. Sorry for the wait, we're keeping an eye on it."
    ),
    "reschedule_request": (
        "Hi {first_name}, the courier couldn't deliver order {order_id} today. "
        "Reply with a convenient day and time (morning, afternoon or evening), "
        "or reply PICKUP to collect it from the nearest point."
    ),
    "address_confirm": (
        "Hi {first_name}, the courier needs a fuller address for order {order_id}. "
        "Please reply with your house or flat number, building, street, area and pincode."
    ),
    "reattempt_confirmed": (
        "Thanks {first_name}! Order {order_id} will be delivered on {date} ({window})."
    ),
    "investigating": (
        "Hi {first_name}, we've noticed order {order_id} isn't moving as expected. "
        "We've raised it with the courier and will update you within {hours} hours."
    ),
}


def template_fields(template_id: str) -> set[str]:
    return {f for _, f, _, _ in string.Formatter().parse(TEMPLATES[template_id]) if f}


@dataclass
class Outbox:
    """Mock SMS / WhatsApp / email gateway. Records what would have been sent."""

    sent: list[dict] = field(default_factory=list)

    def deliver(self, channel: str, to: str, body: str) -> str:
        msg_id = f"MSG-{len(self.sent) + 1:05d}"
        self.sent.append(dict(id=msg_id, channel=channel, to=to, body=body, at=clock.now()))
        return msg_id


class Messenger:
    def __init__(self, orders: OrderStore, cases: CaseStore, outbox: Outbox | None = None):
        self.orders = orders
        self.cases = cases
        self.outbox = outbox or Outbox()

    def send(self, shipment_id: str, customer_ref: str, channel: str, template_id: str,
             variables: dict, dedupe_key: str | None = None) -> ActionResult:
        if template_id not in TEMPLATES:
            return ActionResult(ok=False, action="send_customer_message", detail=f"Unknown template {template_id!r}.")
        missing = template_fields(template_id) - variables.keys()
        if missing:
            return ActionResult(ok=False, action="send_customer_message", detail=f"Missing variables: {sorted(missing)}")

        # Idempotency: the same template is sent at most once per case unless a
        # distinct dedupe_key is given (e.g. a second reminder).
        key = dedupe_key or template_id
        for m in self.cases.get(shipment_id).messages_sent:
            if m.template_id == key:
                return ActionResult(ok=True, action="send_customer_message", detail="Already sent; skipped duplicate.")

        body = TEMPLATES[template_id].format(**variables)
        to = self.orders.contact_for(customer_ref, channel)
        msg_id = self.outbox.deliver(channel, to, body)
        self.cases.record_message(shipment_id, SentMessage(template_id=key, channel=channel, sent_at=clock.now(), rendered=body))
        return ActionResult(ok=True, action="send_customer_message", reference=msg_id, detail=f"Sent via {channel}.")
