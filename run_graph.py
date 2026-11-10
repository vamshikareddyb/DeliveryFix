"""Phase 2 demo: run every seed shipment through the case graph.

  python run_graph.py             # scripted diagnoses (no API key needed)
  python run_graph.py --llm       # real investigator + diagnoser (needs ANTHROPIC_API_KEY)
  python run_graph.py --sim       # force the dependency-free simulator instead of LangGraph
"""
import logging
import sys

from shipment_agent.graph import LLMBrain, ScriptedBrain, recheck_case, start_case
from shipment_agent.scenarios import SCENARIOS
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

print(f"Runner: {runner}; brain: {type(brain).__name__}\n")

correct = 0
for sc in SCENARIOS:
    s = start_case(graph, sc.awb, sc.carrier, sc.order_id, "flagged by monitor")
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
    print()

print(f"Routing matched expected action: {correct}/{len(SCENARIOS)}\n")

print("Recheck loop for DL1000000002 (still delayed each time):")
for i in range(3):
    s = recheck_case(graph, "DL1000000002")
    print(f"  recheck {i + 1}: status={s['status']} recheck_count={s.get('recheck_count')}")

print(f"\nCustomer messages sent: {len(ctx.outbox.sent)}")
for m in ctx.outbox.sent:
    print(f"  [{m['channel']}] {m['body']}")
print(f"Support tickets: {[t['ref'] + ' ' + t['priority'] for t in ctx.backoffice.tickets]}")
