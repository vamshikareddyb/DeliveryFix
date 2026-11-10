"""Rule-based exception detection. Runs over the tracking DB; never calls an LLM.

Only shipments flagged here cost tokens. Each flag carries one primary rule
(the most specific match) plus every rule that matched, for the case trigger.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from . import clock
from .config import SETTINGS, Settings
from .schemas import NormalizedStatus as S
from .tracking_db import ShipmentRow, TrackingDB

# Most specific first: the first match becomes the primary rule.
RULE_ORDER = ["damaged", "delivery_failed", "unknown_status", "customs_hold", "stale_scan", "past_edd"]


@dataclass
class Flag:
    shipment_id: str
    order_id: str
    carrier: str
    rule: str
    rules: list[str]
    detail: str

    @property
    def trigger(self) -> str:
        return f"{self.rule}: {self.detail}"


def evaluate(row: ShipmentRow, now: datetime, settings: Settings = SETTINGS) -> Flag | None:
    status = row.current_status
    if status in (S.DELIVERED.value, S.RTO_INITIATED.value) or row.last_scan_at is None:
        return None
    last_scan = datetime.fromisoformat(row.last_scan_at)
    hours = (now - last_scan).total_seconds() / 3600
    edd = datetime.fromisoformat(row.expected_delivery) if row.expected_delivery else None

    matched: dict[str, str] = {}
    if status == S.DAMAGED.value:
        matched["damaged"] = "carrier reported damage"
    if status == S.DELIVERY_FAILED.value:
        matched["delivery_failed"] = f"delivery attempt failed (attempt {row.attempt_count})"
    if status == S.UNKNOWN.value:
        matched["unknown_status"] = "unrecognised carrier status"
    if status == S.CUSTOMS_HOLD.value:
        matched["customs_hold"] = "held at customs"
    if hours >= settings.stale_scan_hours:
        matched["stale_scan"] = f"no scan for {hours:.0f}h"
    if edd and now > edd:
        matched["past_edd"] = f"{(now - edd) / timedelta(days=1):.1f} days past expected delivery"

    if not matched:
        return None
    rules = [r for r in RULE_ORDER if r in matched]
    return Flag(row.shipment_id, row.order_id, row.carrier, rules[0], rules,
                "; ".join(matched[r] for r in rules))


def scan(db: TrackingDB, now: datetime | None = None, settings: Settings = SETTINGS) -> list[Flag]:
    now = now or clock.now()
    flags = [f for row in db.active_shipments() if (f := evaluate(row, now, settings))]
    # Most urgent first, so a per-cycle budget spends tokens where they matter most.
    return sorted(flags, key=lambda f: RULE_ORDER.index(f.rule))
