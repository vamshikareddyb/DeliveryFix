"""Maps each carrier's raw status text to NormalizedStatus.

Unmapped statuses become UNKNOWN and are recorded in UNMAPPED so someone can
add them. In production this table would live in the database.
"""
from __future__ import annotations

import logging

from ..schemas import NormalizedStatus as S

log = logging.getLogger(__name__)

STATUS_MAP: dict[str, dict[str, S]] = {
    "delhivery": {
        "Manifested": S.PICKED_UP,
        "Picked Up": S.PICKED_UP,
        "In Transit": S.IN_TRANSIT,
        "Reached Destination Hub": S.IN_TRANSIT,
        "Dispatched": S.OUT_FOR_DELIVERY,
        "Pending": S.DELIVERY_FAILED,
        "Consignee Unavailable": S.DELIVERY_FAILED,
        "Address Incomplete": S.DELIVERY_FAILED,
        "Delivered": S.DELIVERED,
        "RTO Initiated": S.RTO_INITIATED,
        "Shipment Damaged": S.DAMAGED,
    },
    "bluedart": {
        "SHIPMENT PICKED UP": S.PICKED_UP,
        "IN TRANSIT": S.IN_TRANSIT,
        "SHIPMENT ARRIVED": S.IN_TRANSIT,
        "SHIPMENT OUT FOR DELIVERY": S.OUT_FOR_DELIVERY,
        "CONSIGNEE NOT AVAILABLE": S.DELIVERY_FAILED,
        "INCOMPLETE ADDRESS & CONTACT DETAILS": S.DELIVERY_FAILED,
        "SHIPMENT DELIVERED": S.DELIVERED,
        "RETURN TO ORIGIN": S.RTO_INITIATED,
        "SHIPMENT FOUND DAMAGED": S.DAMAGED,
        "HELD AT CUSTOMS": S.CUSTOMS_HOLD,
    },
}

UNMAPPED: set[tuple[str, str]] = set()


def normalize(carrier: str, raw_status: str) -> S:
    status = STATUS_MAP.get(carrier, {}).get(raw_status)
    if status is None:
        UNMAPPED.add((carrier, raw_status))
        log.warning("Unmapped status %r from carrier %r", raw_status, carrier)
        return S.UNKNOWN
    return status
