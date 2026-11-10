"""Phase 4 demo: the monitor running over three simulated days.

  python run_monitor.py           # scripted diagnoses, in-memory
  python run_monitor.py --llm     # real LLM for investigation/diagnosis/reply parsing
  python run_monitor.py --sim     # force the plain-Python runner even if LangGraph is installed

Every scheduler cycle prints one line: what was polled, flagged, started,
skipped as a duplicate, rechecked, and how many runs touched the LLM.
"""
import logging
import sys
from datetime import timedelta

from shipment_agent import clock
from shipment_agent.carriers.resilient import resilient_carriers
from shipment_agent.graph import LLMBrain, ScriptedBrain
from shipment_agent.scenarios import BY_AWB, SCENARIOS
from shipment_agent.scheduler import Monitor, register_scenarios
from shipment_agent.service import CaseService
from shipment_agent.tools import ToolContext
from shipment_agent.tracking_db import TrackingDB

logging.basicConfig(level=logging.ERROR)

ctx = ToolContext()
ctx.carriers = resilient_carriers(ctx.carriers)
brain = LLMBrain(ctx) if "--llm" in sys.argv else ScriptedBrain(ctx)
try:
    if "--sim" in sys.argv:
        raise ImportError
    from shipment_agent.graph import build_graph

    graph, runner = build_graph(ctx, brain), "LangGraph"
except ImportError:
    from shipment_agent.graph import build_simulated_graph

    graph, runner = build_simulated_graph(ctx, brain), "simulator (LangGraph not installed)"

db = TrackingDB()
register_scenarios(db, SCENARIOS)
svc = CaseService(graph, ctx, db)
monitor = Monitor(svc)
start = clock.now()
phone = lambda awb: ctx.orders.contact_for(ctx.orders.get(BY_AWB[awb].order_id).customer_ref, "sms")  # noqa: E731


def at(hours: float, note: str):
    clock.freeze(start + timedelta(hours=hours))
    print(f"\n+{hours:>4.0f}h  {note}")


print(f"Runner: {runner}; brain: {type(brain).__name__}; {len(SCENARIOS)} shipments registered")

at(0, "first cycle")
print("       " + monitor.run_cycle().summary())

at(0.25, "second cycle 15 min later (nothing new should start)")
print("       " + monitor.run_cycle().summary())

at(2, "Priya replies on WhatsApp")
r = svc.handle_inbound_message(phone("BD2000000003"), "Please come tomorrow evening")
print(f"       -> case {r.shipment_id}: {r.status}")

at(3, "Blue Dart webhook: BD2000000006 (awaiting reship approval) is delivered after all")
ctx.carriers["bluedart"].add_scan("BD2000000006", "SHIPMENT DELIVERED", "Chennai")
print(f"       -> {monitor.on_webhook('bluedart', 'BD2000000006', 'SHIPMENT DELIVERED', 'Chennai')}")

at(25, "cycle: Karan never replied; the delay case is due for a recheck")
print("       " + monitor.run_cycle().summary())

at(27, "Delhivery API is flaky for a while (2 timeouts), cycle still completes")
ctx.carriers["delhivery"].inner.fail_next = 2
rep = monitor.run_cycle()
print("       " + rep.summary())
print(f"       Delhivery retries so far: {ctx.carriers['delhivery'].stats.retries}")

at(30, "Blue Dart webhook: BD2000000003 delivered on the rebooked slot")
ctx.carriers["bluedart"].add_scan("BD2000000003", "SHIPMENT DELIVERED", "New Delhi Saket")
print(f"       -> {monitor.on_webhook('bluedart', 'BD2000000003', 'SHIPMENT DELIVERED', 'New Delhi Saket')}")

at(50, "cycle: Karan's reminder window has passed too")
print("       " + monitor.run_cycle().summary())

at(73, "cycle: the lost-parcel inquiry is due for its recheck")
print("       " + monitor.run_cycle().summary())

print("\nFinal case registry:")
for c in db.cases():
    print(f"  {c.shipment_id}  {c.status:<18} runs={c.runs}  next_check={c.next_check_at or '-'}")
print(f"\nMessages sent: {len(ctx.outbox.sent)}; tickets: {len(ctx.backoffice.tickets)}; "
      f"reshipments: {len(ctx.backoffice.reshipments)}; refunds: {len(ctx.backoffice.refunds)}")
