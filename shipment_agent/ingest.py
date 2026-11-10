"""Getting scans into the tracking DB: carrier webhooks (push) and polling (pull).

Both paths store events the same way, and the UNIQUE constraint on events
means a scan that arrives by webhook and again by poll is stored once.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime

from .carriers import CarrierAPIError
from .carriers.status_map import normalize
from .schemas import TrackingEvent
from .tracking_db import TrackingDB

log = logging.getLogger(__name__)


@dataclass
class PollReport:
    polled: int = 0
    new_events: int = 0
    errors: list[str] = field(default_factory=list)


def ingest_webhook(db: TrackingDB, carrier: str, awb: str, raw_status: str, location: str,
                   event_time: datetime, remarks: str | None = None) -> TrackingEvent | None:
    """Handle one carrier webhook. Returns the stored event, or None if unknown shipment or duplicate."""
    if db.shipment(awb) is None:
        log.warning("Webhook for unknown shipment %s from %s", awb, carrier)
        return None
    ev = TrackingEvent(raw_status=raw_status, normalized_status=normalize(carrier, raw_status),
                       location=location, remarks=remarks, event_time=event_time)
    return ev if db.add_event(awb, ev, source="webhook") else None


def poll_active(db: TrackingDB, carriers: dict) -> PollReport:
    """Fallback for missed webhooks: fetch tracking for every non-terminal shipment.

    `carriers` should be ResilientCarrier-wrapped so retries and rate limits apply.
    One carrier failing doesn't stop the others.
    """
    report = PollReport()
    for row in db.active_shipments():
        try:
            t = carriers[row.carrier].get_tracking(row.shipment_id)
        except (CarrierAPIError, KeyError) as e:
            report.errors.append(f"{row.shipment_id}: {e}")
            continue
        report.polled += 1
        db.set_expected_delivery(row.shipment_id, t.expected_delivery_date)
        for ev in t.events:
            if db.add_event(row.shipment_id, ev, source="poll"):
                report.new_events += 1
    return report
