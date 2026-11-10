"""Phase 2 demo: run every seed shipment through the case graph.

  python run_graph.py             # scripted diagnoses (no API key needed)
  python run_graph.py --llm       # real investigator + diagnoser (needs ANTHROPIC_API_KEY)
  python run_graph.py --sim       # force the dependency-free simulator instead of LangGraph
"""
import logging
import sys

from datetime import timedelta

from shipment_agent import clock
from shipment_agent.graph import LLMBrain, ScriptedBrain, pending_interrupt, recheck_case
from shipment_agent.scenarios import SCENARIOS
from shipment_agent.service import CaseService
from shipment_agent.tools import ToolContext

logging.basicConfig(level=logging.ERROR)

ctx = ToolContext()
brain = LLMBrain(ctx) if "--llm" in sys.argv else ScriptedBrain(ctx)

try:
    if "--sim" in sys.argv:
        raise ImportError
    from shipment_agent.graph import build_graph

    graph, runner = build_graph(ctx, brain), "LangGraph"
except ImportError:
    from shipment_agent.graph import build_simulated_graph

    graph, runner = build_simulated_graph(ctx, brain), "simulator (LangGraph not installed)"

svc = CaseService(graph, ctx)
print(f"Runner: {runner}; brain: {type(brain).__name__}\n")

correct = 0
for sc in SCENARIOS:
    s = svc.start(sc.awb, sc.carrier, sc.order_id, "flagged by monitor")
    d = s["diagnosis"]
    ok = d["recommended_action"] == sc.expected_action
    correct += ok
    print(f"{sc.awb} {sc.description}")
    print(f"  {d['issue_type']} ({d['confidence']:.2f}) -> {d['recommended_action']}  "
          f"=> status {s['status']}  {'✓' if ok else '✗ expected ' + sc.expected_action}")
    for o in s.get("guardrail_overrides", []):
        print(f"  guardrail: {o}")
    for a in s.get("actions", []):
        print(f"  action: {a['action']} {a.get('reference') or ''} {a['detail']}")
    if s.get("proposal"):
        p = s["proposal"]
        print(f"  proposal: {p['kind']} ₹{p['amount']:,.0f}, needs {p['approver_level']} approval")
    waiting = pending_interrupt(graph, sc.awb)
    if waiting:
        print(f"  ⏸ paused, waiting for: {waiting['kind']}")
    print()

print(f"Routing matched expected action: {correct}/{len(SCENARIOS)}\n")

print("Recheck loop for DL1000000002 (still delayed each time):")
for i in range(3):
    s = recheck_case(graph, "DL1000000002")
    print(f"  recheck {i + 1}: status={s['status']} recheck_count={s.get('recheck_count')}")


print("\n--- Phase 3: people answer ---")
phone = lambda awb: ctx.orders.contact_for(ctx.orders.get(next(x.order_id for x in SCENARIOS if x.awb == awb)).customer_ref, "sms")

r = svc.handle_inbound_message(phone("BD2000000003"), "Please come tomorrow evening")
print(f"Priya replies 'Please come tomorrow evening' -> {r.status}")

print(f"Karan doesn't reply. Timeouts fired 25h later: {svc.fire_due_timeouts(clock.now() + timedelta(hours=25))}")
clock.freeze(clock.now() + timedelta(hours=25))
r = svc.handle_inbound_message(phone("DL1000000004"), "Flat 3B, Sea Breeze Apartments, Marine Drive, 400020")
print(f"...then sends his address -> {r.status}")

for p in svc.pending_approvals():
    print(f"Approval queue: {p['shipment_id']} {p['proposal']['kind']} ₹{p['proposal']['amount']:,.0f} "
          f"({p['proposal']['approver_level']})")
s = svc.decide("BD2000000006", "agent.rao", "agent", approved=True)
print(f"agent.rao approves the reship for BD2000000006 -> {s['status']}")

print(f"\nCustomer messages sent: {len(ctx.outbox.sent)}")
for m in ctx.outbox.sent:
    print(f"  [{m['channel']}] {m['body']}")
print(f"Support tickets: {[t['ref'] + ' ' + t['priority'] for t in ctx.backoffice.tickets]}")
print(f"Reshipments: {[(r['ref'], r['approved_by']) for r in ctx.backoffice.reshipments]}")
