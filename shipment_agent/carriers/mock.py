"""In-memory carrier that serves the seed scenarios.

Supports simulated flakiness (failure_rate) so retry policies can be tested
in Phase 4, and records every action call so tests can assert on side effects.
"""
from __future__ import annotations

import random
import uuid
from datetime import timedelta

from .. import clock
from ..scenarios import BY_AWB, Scenario
from ..schemas import (
    ActionResult,
    Address,
    CarrierNotes,
    DeliverySlot,
    NormalizedStatus,
    TrackingEvent,
    TrackingResult,
)
from .base import CarrierAPIError
from .status_map import normalize

# What each mock carrier allows. Mirrors what the policy docs say.
CAPABILITIES = {
    "delhivery": {"address_change_in_transit": True, "max_reattempts": 3},
    "bluedart": {"address_change_in_transit": False, "max_reattempts": 2},
}


class MockCarrierAdapter:
    def __init__(self, name: str, failure_rate: float = 0.0, seed: int | None = None):
        self.name = name
        self.failure_rate = failure_rate
        self._rng = random.Random(seed)
        self.calls: list[tuple[str, dict]] = []  # action log for tests
        self._reattempts: dict[str, int] = {}
        self.extra_scans: dict[str, list[TrackingEvent]] = {}  # scans that "happen" after the scenario starts
        self.fail_next = 0  # deterministic flakiness: fail the next N calls
        # Scenario scans are "N hours before the adapter was created", fixed from then on,
        # so time passing makes them older (as with a real carrier) instead of moving with the clock.
        self.anchor = clock.now()

    # ---------------- helpers ----------------
    def _maybe_fail(self):
        if self.fail_next > 0:
            self.fail_next -= 1
            raise CarrierAPIError(f"{self.name}: simulated timeout")
        if self.failure_rate and self._rng.random() < self.failure_rate:
            raise CarrierAPIError(f"{self.name}: simulated 503 Service Unavailable")

    def _scenario(self, awb: str) -> Scenario:
        sc = BY_AWB.get(awb)
        if sc is None or sc.carrier != self.name:
            raise KeyError(f"AWB {awb} not found at carrier {self.name}")
        return sc

    def _events(self, sc: Scenario) -> list[TrackingEvent]:
        now = self.anchor
        events = [
            TrackingEvent(
                raw_status=s.raw_status,
                normalized_status=normalize(self.name, s.raw_status),
                location=s.location,
                remarks=s.remarks,
                event_time=now - timedelta(hours=s.hours_ago),
            )
            for s in sc.scans
        ] + self.extra_scans.get(sc.awb, [])
        return sorted(events, key=lambda e: e.event_time, reverse=True)

    def add_scan(self, awb: str, raw_status: str, location: str, remarks: str | None = None) -> TrackingEvent:
        """Simulate a new scan arriving now. Returns the event (feed it to the webhook handler)."""
        self._scenario(awb)
        ev = TrackingEvent(raw_status=raw_status, normalized_status=normalize(self.name, raw_status),
                           location=location, remarks=remarks, event_time=clock.now())
        self.extra_scans.setdefault(awb, []).append(ev)
        return ev

    # ---------------- read ----------------
    def get_tracking(self, awb: str, refresh: bool = False) -> TrackingResult:
        self._maybe_fail()
        sc = self._scenario(awb)
        events = self._events(sc)
        latest = events[0]
        now = clock.now()
        edd = self.anchor + timedelta(days=sc.edd_days_from_now)
        return TrackingResult(
            awb=awb,
            carrier=self.name,
            current_status=latest.normalized_status,
            last_scan_at=latest.event_time,
            hours_since_last_scan=round((now - latest.event_time).total_seconds() / 3600, 1),
            expected_delivery_date=edd,
            is_past_edd=now > edd and latest.normalized_status != NormalizedStatus.DELIVERED,
            attempt_count=sum(e.normalized_status == NormalizedStatus.DELIVERY_FAILED for e in events),
            events=events,
        )

    def get_notes(self, awb: str) -> CarrierNotes:
        self._maybe_fail()
        sc = self._scenario(awb)
        return CarrierNotes(awb=awb, carrier=self.name, notes=[s.remarks for s in sc.scans if s.remarks])

    # ---------------- actions ----------------
    def request_reattempt(self, awb: str, slot: DeliverySlot) -> ActionResult:
        self._maybe_fail()
        self._scenario(awb)
        used = self._reattempts.get(awb, 0)
        if used >= CAPABILITIES[self.name]["max_reattempts"]:
            return ActionResult(ok=False, action="request_reattempt", detail="Reattempt limit reached; shipment will go RTO.")
        self._reattempts[awb] = used + 1
        self.calls.append(("request_reattempt", {"awb": awb, "slot": slot.model_dump()}))
        return ActionResult(ok=True, action="request_reattempt", reference=f"RA-{uuid.uuid4().hex[:8]}",
                            detail=f"Reattempt booked for {slot.date} ({slot.window}).")

    def update_address(self, awb: str, address: Address) -> ActionResult:
        self._maybe_fail()
        self._scenario(awb)
        if not CAPABILITIES[self.name]["address_change_in_transit"]:
            return ActionResult(ok=False, action="update_address",
                                detail=f"{self.name} does not allow address changes in transit.")
        self.calls.append(("update_address", {"awb": awb, "address": address.model_dump()}))
        return ActionResult(ok=True, action="update_address", reference=f"AC-{uuid.uuid4().hex[:8]}",
                            detail="Address updated with carrier.")

    def raise_inquiry(self, awb: str, issue: str) -> ActionResult:
        self._maybe_fail()
        self._scenario(awb)
        self.calls.append(("raise_inquiry", {"awb": awb, "issue": issue}))
        return ActionResult(ok=True, action="raise_inquiry", reference=f"INQ-{uuid.uuid4().hex[:8]}",
                            detail="Trace request opened with carrier. Typical response time 48-72h.")


def default_carriers(**kwargs) -> dict[str, MockCarrierAdapter]:
    return {name: MockCarrierAdapter(name, **kwargs) for name in CAPABILITIES}
