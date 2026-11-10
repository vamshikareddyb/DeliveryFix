"""Phase 4: tracking DB, detection, dedupe, scheduling, retries, persistence."""
import logging
from datetime import timedelta

import pytest

from shipment_agent import clock, detector
from shipment_agent.carriers import CarrierAPIError
from shipment_agent.carriers.mock import MockCarrierAdapter
from shipment_agent.carriers.resilient import ResilientCarrier, RetryPolicy, TokenBucket, resilient_carriers
from shipment_agent.config import Settings
from shipment_agent.graph import ScriptedBrain, build_simulated_graph, pending_interrupt
from shipment_agent.graph.persist import SqliteThreadStore
from shipment_agent.ingest import ingest_webhook, poll_active
from shipment_agent.scenarios import BY_AWB, SCENARIOS
from shipment_agent.scheduler import Monitor, register_scenarios
from shipment_agent.schemas import DeliverySlot
from shipment_agent.service import CaseService
from shipment_agent.tools import ToolContext
from shipment_agent.tracking_db import TrackingDB

from .conftest import NOW

logging.disable(logging.WARNING)
NO_SLEEP = lambda s: None  # noqa: E731


def setup(settings=None, brain_cls=ScriptedBrain, db_path=":memory:", store=None, ctx=None):
    ctx = ctx or ToolContext()
    if not isinstance(next(iter(ctx.carriers.values())), ResilientCarrier):
        ctx.carriers = resilient_carriers(ctx.carriers, sleep=NO_SLEEP)
    db = TrackingDB(db_path)
    register_scenarios(db, SCENARIOS)
    kw = {"settings": settings} if settings else {}
    g = build_simulated_graph(ctx, brain_cls(ctx), store=store, **kw)
    svc = CaseService(g, ctx, db)
    return ctx, db, svc, Monitor(svc, **kw)


def phone(ctx, awb):
    return ctx.orders.contact_for(ctx.orders.get(BY_AWB[awb].order_id).customer_ref, "sms")


# ---------------------------------------------------------------- ingestion
def test_poll_and_webhook_store_each_scan_once():
    ctx, db, _, _ = setup()
    r = poll_active(db, ctx.carriers)
    assert r.polled == 8 and r.new_events == 22
    sc = BY_AWB["BD2000000003"].scans[-1]
    dup = ingest_webhook(db, "bluedart", "BD2000000003", sc.raw_status, sc.location,
                         NOW - timedelta(hours=sc.hours_ago), sc.remarks)
    assert dup is None
    assert poll_active(db, ctx.carriers).new_events == 0


def test_webhook_for_unknown_shipment_is_ignored():
    _, db, _, _ = setup()
    assert ingest_webhook(db, "delhivery", "DL9999999999", "Delivered", "X", NOW) is None


def test_shipment_row_tracks_latest_scan():
    ctx, db, _, _ = setup()
    poll_active(db, ctx.carriers)
    row = db.shipment("BD2000000003")
    assert row.current_status == "delivery_failed" and row.attempt_count == 1
    assert db.shipment("DL1000000007").current_status == "delivered"
    assert {r.shipment_id for r in db.active_shipments()} == {s.awb for s in SCENARIOS} - {"DL1000000007"}


# ---------------------------------------------------------------- detection
def test_detector_rules():
    ctx, db, _, _ = setup()
    poll_active(db, ctx.carriers)
    flags = {f.shipment_id: f for f in detector.scan(db, NOW)}
    assert set(flags) == {"DL1000000002", "BD2000000003", "DL1000000004", "DL1000000005", "BD2000000006", "BD2000000008"}
    assert flags["BD2000000006"].rule == "damaged"
    assert flags["BD2000000003"].rule == "delivery_failed"
    assert flags["BD2000000008"].rule == "unknown_status"
    assert flags["DL1000000002"].rules == ["stale_scan", "past_edd"]
    assert "no scan for 230h" in flags["DL1000000005"].trigger
    assert [f.rule for f in detector.scan(db, NOW)][0] == "damaged"  # most urgent first


# ---------------------------------------------------------------- cycles and dedupe
def test_cycle_starts_flagged_cases_once():
    _, db, svc, m = setup()
    r1 = m.run_cycle()
    assert len(r1.started) == 6 and r1.llm_runs == 6
    assert db.case("DL1000000001") is None and db.case("DL1000000007") is None  # healthy / delivered: no tokens
    r2 = m.run_cycle()
    assert r2.started == [] and len(r2.skipped) == 6 and r2.llm_runs == 0
    assert db.case_counts() == {"awaiting_approval": 1, "escalated": 1, "monitoring": 2, "waiting_customer": 2}


def test_budget_defers_and_catches_up():
    _, db, _, m = setup(Settings(llm_runs_per_cycle=2))
    r1 = m.run_cycle()
    assert r1.started == ["BD2000000006", "BD2000000003"]  # damaged, then failed delivery
    assert len(r1.deferred) == 4
    r2 = m.run_cycle()
    assert len(r2.started) == 2 and len(r2.skipped) == 2
    m.run_cycle()
    assert sum(db.case_counts().values()) == 6


def test_due_rechecks_and_timeouts_run_on_schedule():
    ctx, db, svc, m = setup()
    m.run_cycle()
    assert m.run_cycle(NOW + timedelta(hours=1)).rechecks == []
    clock.freeze(NOW + timedelta(hours=25))
    r = m.run_cycle()
    assert r.rechecks == ["DL1000000002"]          # delay: 24h recheck; lost inquiry waits 72h
    assert r.timeouts == ["BD2000000003", "DL1000000004"]
    assert sum("quick reminder" in msg["body"] for msg in ctx.outbox.sent) == 2
    clock.freeze(NOW + timedelta(hours=73))
    assert "DL1000000005" in m.run_cycle().rechecks


# ---------------------------------------------------------------- event-driven wake-ups
def test_delivered_scan_closes_waiting_case():
    ctx, db, svc, m = setup()
    m.run_cycle()
    ctx.carriers["bluedart"].add_scan("BD2000000003", "SHIPMENT DELIVERED", "New Delhi Saket")
    out = m.on_webhook("bluedart", "BD2000000003", "SHIPMENT DELIVERED", "New Delhi Saket")
    assert out == {"stored": True, "status": "delivered", "woke": "closed"}
    assert pending_interrupt(svc.graph, "BD2000000003") is None
    r = svc.handle_inbound_message(phone(ctx, "BD2000000003"), "tomorrow evening")
    assert not r.handled  # nothing to rebook any more


def test_delivered_while_awaiting_approval_cancels_reship():
    ctx, db, svc, m = setup()
    m.run_cycle()
    assert [p["shipment_id"] for p in svc.pending_approvals()] == ["BD2000000006"]
    ctx.carriers["bluedart"].add_scan("BD2000000006", "SHIPMENT DELIVERED", "Chennai")
    m.on_webhook("bluedart", "BD2000000006", "SHIPMENT DELIVERED", "Chennai")
    assert svc.pending_approvals() == [] and db.case("BD2000000006").status == "closed"
    assert ctx.backoffice.reshipments == []


def test_second_failed_attempt_asks_again():
    ctx, db, svc, m = setup()
    m.run_cycle()
    svc.handle_inbound_message(phone(ctx, "BD2000000003"), "tomorrow evening")
    assert db.case("BD2000000003").status == "monitoring"
    clock.freeze(NOW + timedelta(hours=30))
    ctx.carriers["bluedart"].add_scan("BD2000000003", "CONSIGNEE NOT AVAILABLE", "New Delhi Saket", "Not home again")
    out = m.on_webhook("bluedart", "BD2000000003", "CONSIGNEE NOT AVAILABLE", "New Delhi Saket", remarks="Not home again")
    assert out["woke"] == "waiting_customer"
    assert sum("couldn't deliver" in msg["body"] for msg in ctx.outbox.sent) == 2


def test_minor_scan_does_not_wake():
    ctx, db, svc, m = setup()
    m.run_cycle()
    ctx.carriers["delhivery"].add_scan("DL1000000002", "In Transit", "Hyderabad Hub")
    assert m.on_webhook("delhivery", "DL1000000002", "In Transit", "Hyderabad Hub")["woke"] is None


# ---------------------------------------------------------------- retries and rate limits
def test_reads_retry_with_backoff():
    sleeps = []
    c = ResilientCarrier(MockCarrierAdapter("delhivery"), sleep=sleeps.append,
                         bucket=TokenBucket(1000, 1000, sleep=NO_SLEEP))
    c.inner.fail_next = 2
    assert c.get_tracking("DL1000000002").current_status.value == "in_transit"
    assert c.stats.retries == 2 and len(sleeps) == 2 and sleeps[1] > sleeps[0]


def test_reads_give_up_after_max_attempts():
    c = ResilientCarrier(MockCarrierAdapter("delhivery"), retry=RetryPolicy(max_attempts=3), sleep=NO_SLEEP)
    c.inner.fail_next = 10
    with pytest.raises(CarrierAPIError):
        c.get_tracking("DL1000000002")
    assert c.stats.calls == 3 and c.stats.failures == 1


def test_actions_are_not_retried():
    c = ResilientCarrier(MockCarrierAdapter("bluedart"), sleep=NO_SLEEP)
    c.inner.fail_next = 1
    r = c.request_reattempt("BD2000000003", DeliverySlot(date="2026-11-11", window="evening"))
    assert not r.ok and "not retried" in r.detail
    assert c.stats.calls == 1 and c.inner.calls == []


def test_carrier_outage_during_rebook_escalates():
    ctx, db, svc, m = setup()
    m.run_cycle()
    ctx.carriers["bluedart"].inner.fail_next = 1
    r = svc.handle_inbound_message(phone(ctx, "BD2000000003"), "tomorrow evening")
    assert r.status == "escalated"
    assert "not retried automatically" in ctx.backoffice.tickets[-1]["summary"]


def test_poll_survives_a_carrier_outage():
    ctx, db, _, _ = setup()
    ctx.carriers["bluedart"].inner.failure_rate = 1.0
    r = poll_active(db, ctx.carriers)
    assert r.polled == 5 and len(r.errors) == 3  # Delhivery fine, Blue Dart down


def test_token_bucket_paces_calls():
    t = [0.0]
    b = TokenBucket(rate_per_sec=1, burst=2, now=lambda: t[0], sleep=lambda s: t.__setitem__(0, t[0] + s))
    for _ in range(5):
        b.acquire()
    assert b.waited == pytest.approx(3.0)


# ---------------------------------------------------------------- robustness
class _FlakyBrain(ScriptedBrain):
    broken = {"DL1000000004"}

    def diagnose(self, report, awb, *a):
        if awb in self.broken:
            raise ValueError("model returned invalid JSON")
        return super().diagnose(report, awb, *a)


def test_one_failing_case_does_not_stop_the_cycle():
    _, db, _, m = setup(brain_cls=_FlakyBrain)
    r = m.run_cycle()
    assert len(r.started) == 5 and r.errors == ["DL1000000004: ValueError: model returned invalid JSON"]
    assert db.case("DL1000000004").status == "error"
    _FlakyBrain.broken = set()
    try:
        assert "DL1000000004" in m.run_cycle().started  # retried next cycle
    finally:
        _FlakyBrain.broken = {"DL1000000004"}


def test_cases_survive_a_restart(tmp_path):
    db_path, store_path = str(tmp_path / "tracking.db"), str(tmp_path / "threads.db")
    ctx, db, svc, m = setup(db_path=db_path, store=SqliteThreadStore(store_path))
    m.run_cycle()

    # "Restart": brand-new graph, service and monitor objects over the same files.
    # (ctx is reused because its order/case/outbox stores are in-memory mocks;
    # in production those are databases too.)
    _, db2, svc2, m2 = setup(db_path=db_path, store=SqliteThreadStore(store_path), ctx=ctx)
    assert db2.case_counts() == db.case_counts()
    assert svc2.pending_approvals()[0]["shipment_id"] == "BD2000000006"
    r = svc2.handle_inbound_message(phone(ctx, "BD2000000003"), "tomorrow evening")
    assert r.handled and r.status == "monitoring"
    assert m2.run_cycle().started == []  # no duplicate cases after restart


# ---------------------------------------------------------------- no repeated questions
def test_recheck_after_rebooking_waits_then_chases_carrier():
    ctx, db, svc, m = setup()
    m.run_cycle()
    svc.handle_inbound_message(phone(ctx, "BD2000000003"), "tomorrow evening")  # slot 2026-11-11
    clock.freeze(NOW + timedelta(hours=25))  # recheck due, slot not yet passed (IST 11 Nov)
    m.run_cycle()
    s = svc.state("BD2000000003")
    assert s["status"] == "monitoring"
    assert any("-> monitor: reattempt booked for 2026-11-11" in o for o in s["guardrail_overrides"])
    assert sum("couldn't deliver" in msg["body"] for msg in ctx.outbox.sent) == 1

    clock.freeze(NOW + timedelta(hours=49))  # 12 Nov: slot passed, no scan at all
    m.run_cycle()
    s = svc.state("BD2000000003")
    assert any("-> carrier_inquiry" in o for o in s["guardrail_overrides"])
    assert ctx.carriers["bluedart"].inner.calls[-1][0] == "raise_inquiry"


def test_unanswered_prepaid_case_does_not_loop_forever():
    ctx, db, svc, m = setup()
    m.run_cycle()
    hours = 0
    for _ in range(12):  # many days of cycles with no reply from Karan
        hours += 25
        clock.freeze(NOW + timedelta(hours=hours))
        m.run_cycle()
    case = db.case("DL1000000004")
    assert case.status == "escalated"
    asks = sum("needs a fuller address" in msg["body"] for msg in ctx.outbox.sent)
    reminders = sum("quick reminder" in msg["body"] and "ORD-0004" in msg["body"] for msg in ctx.outbox.sent)
    assert asks == 1 and reminders == 1


def test_closed_case_is_not_rediagnosed_every_cycle():
    ctx, db, svc, m = setup()
    m.run_cycle()
    clock.freeze(NOW + timedelta(hours=50))  # healthy DL1000000001 is now 56h stale
    assert "DL1000000001" in m.run_cycle().started  # scripted brain calls it fine -> closed
    for h in (51, 60, 90):
        clock.freeze(NOW + timedelta(hours=h))
        assert "DL1000000001 (closed, nothing new)" in m.run_cycle().skipped
    clock.freeze(NOW + timedelta(hours=99))  # cooldown (48h) passed: look again
    assert "DL1000000001" in m.run_cycle().started


def test_closed_case_reopens_on_new_scan():
    ctx, db, svc, m = setup()
    m.run_cycle()
    clock.freeze(NOW + timedelta(hours=50))
    m.run_cycle()
    clock.freeze(NOW + timedelta(hours=52))
    ctx.carriers["delhivery"].add_scan("DL1000000001", "Consignee Unavailable", "Hyderabad", "Door locked")
    m.on_webhook("delhivery", "DL1000000001", "Consignee Unavailable", "Hyderabad", remarks="Door locked")
    assert "DL1000000001" in m.run_cycle().started
