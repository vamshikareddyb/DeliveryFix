"""The monitoring loop: what runs every cycle (e.g. every 15 minutes via cron,
a LangGraph cron job, or a worker), plus the carrier webhook entry point.

One cycle:
  1. poll   - fetch tracking for active shipments (catches missed webhooks)
  2. timeouts - resume cases whose customer reply deadline passed (no LLM)
  3. detect - SQL/rule scan of the tracking DB (no LLM)
  4. start  - open a case per flag, skipping shipments that already have one,
              most urgent first, within the cycle's LLM budget
  5. rechecks - re-run monitoring cases whose next_check_at has passed,
              with whatever budget is left
Anything over budget waits for the next cycle: flags are found again and
due rechecks stay due, so nothing is lost.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from . import clock, detector
from .config import SETTINGS, Settings
from .ingest import ingest_webhook, poll_active
from .service import CaseService
from .tracking_db import ACTIVE_CASE


@dataclass
class CycleReport:
    at: str
    polled: int = 0
    new_events: int = 0
    poll_errors: list[str] = field(default_factory=list)
    timeouts: list[str] = field(default_factory=list)
    flags: list[str] = field(default_factory=list)
    started: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    deferred: list[str] = field(default_factory=list)
    rechecks: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    case_counts: dict = field(default_factory=dict)

    @property
    def llm_runs(self) -> int:
        return len(self.started) + len(self.rechecks)

    def summary(self) -> str:
        return (f"[{self.at}] polled {self.polled} (+{self.new_events} scans, {len(self.poll_errors)} errors) | "
                f"timeouts {len(self.timeouts)} | flags {len(self.flags)}: started {len(self.started)}, "
                f"skipped {len(self.skipped)}, deferred {len(self.deferred)} | rechecks {len(self.rechecks)} | "
                f"errors {len(self.errors)} | LLM runs {self.llm_runs} | cases {self.case_counts}")


class Monitor:
    def __init__(self, svc: CaseService, poll: bool = True, settings: Settings = SETTINGS):
        self.svc, self.poll, self.settings = svc, poll, settings
        self.db = svc.db

    def run_cycle(self, now: datetime | None = None) -> CycleReport:
        now = now or clock.now()
        r = CycleReport(at=now.isoformat(timespec="minutes"))
        budget = self.settings.llm_runs_per_cycle

        if self.poll:
            p = poll_active(self.db, self.svc.ctx.carriers)
            r.polled, r.new_events, r.poll_errors = p.polled, p.new_events, p.errors

        r.timeouts = self.svc.fire_due_timeouts(now)

        for flag in detector.scan(self.db, now, self.settings):
            r.flags.append(f"{flag.shipment_id}:{flag.rule}")
            existing = self.db.case(flag.shipment_id)
            if existing and existing.status in ACTIVE_CASE:
                r.skipped.append(f"{flag.shipment_id} ({existing.status})")
                continue
            if existing and existing.status == "closed" and self._recently_closed(existing, now):
                r.skipped.append(f"{flag.shipment_id} (closed, nothing new)")
                continue
            if len(r.started) >= budget:
                r.deferred.append(flag.shipment_id)
                continue
            d = self.svc.start_from_flag(flag)
            (r.started if d.action == "started" else r.errors).append(
                f"{flag.shipment_id}" + (f": {d.reason}" if d.action == "error" else ""))

        remaining = max(0, budget - len(r.started))
        due = self.db.due_rechecks(now)
        for awb in due[:remaining]:
            if self.svc.recheck(awb) is None:
                r.errors.append(f"{awb}: recheck failed")
            r.rechecks.append(awb)
        r.deferred.extend(due[remaining:])

        r.case_counts = self.db.case_counts()
        return r

    def _recently_closed(self, case, now: datetime) -> bool:
        """A closed case stays closed until a new scan arrives or the cooldown passes,
        so a shipment the agent already judged fine isn't re-diagnosed every cycle."""
        closed_at = datetime.fromisoformat(case.closed_at)
        last = self.db.last_event_received(case.shipment_id)
        new_scan = last is not None and datetime.fromisoformat(last) > closed_at
        return not new_scan and now - closed_at < timedelta(hours=self.settings.reopen_cooldown_hours)

    def on_webhook(self, carrier: str, awb: str, raw_status: str, location: str,
                   event_time: datetime | None = None, remarks: str | None = None) -> dict:
        """Carrier tracking webhook: store the scan, then wake the case if the scan matters."""
        ev = ingest_webhook(self.db, carrier, awb, raw_status, location, event_time or clock.now(), remarks)
        if ev is None:
            return {"stored": False, "woke": None}
        return {"stored": True, "status": ev.normalized_status.value, "woke": self.svc.on_carrier_event(awb, ev)}


def register_scenarios(db, scenarios) -> None:
    """Load the seed shipments into the tracking DB (what order fulfilment does in real life)."""
    for sc in scenarios:
        db.register_shipment(sc.awb, sc.order_id, sc.carrier)
