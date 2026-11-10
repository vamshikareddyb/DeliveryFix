"""Phase 1 demo.

  python demo.py          # no LLM: runs the read tools on every scenario
  python demo.py --llm    # also runs investigator + diagnoser + guardrails
                          # (needs langchain-core, langchain-anthropic, ANTHROPIC_API_KEY)
"""
import sys

from shipment_agent.guardrails import apply_guardrails
from shipment_agent.scenarios import SCENARIOS
from shipment_agent.tools import ReadTools, ToolContext

ctx = ToolContext()
read = ReadTools(ctx)
use_llm = "--llm" in sys.argv

if use_llm:
    from shipment_agent.chains import diagnose, get_llm, run_investigation
    from shipment_agent.tools import build_read_tools

    llm, tools = get_llm(), build_read_tools(ctx)

hits = {}
for sc in SCENARIOS:
    t = read.get_tracking_status(sc.awb, sc.carrier)
    o = read.get_order_details(sc.order_id)
    notes = read.get_carrier_notes(sc.awb, sc.carrier)["notes"]
    print(f"\n{sc.awb} [{sc.carrier}] {sc.description}")
    print(f"  status={t['current_status']}  last scan {t['hours_since_last_scan']}h ago  "
          f"past EDD={t['is_past_edd']}  value=₹{o['order_value']:,.0f} ({o['payment_mode']})")
    if notes:
        print(f"  notes: {notes}")
    print(f"  expected: {sc.expected_issue} -> {sc.expected_action}")

    if use_llm:
        trigger = "flagged by monitor"
        report = run_investigation(llm, tools, sc.awb, sc.carrier, sc.order_id, trigger)
        d = diagnose(llm, report, sc.awb, sc.carrier, sc.order_id, trigger)
        tracking = ctx.carriers[sc.carrier].get_tracking(sc.awb)
        d2, overrides = apply_guardrails(d, ctx.orders.get(sc.order_id), tracking, ctx.cases.get(sc.awb))
        ok = (d2.issue_type == sc.expected_issue) if sc.expected_issue else True
        hits[sc.awb] = ok
        print(f"  LLM: {d.issue_type} ({d.confidence:.2f}) -> {d.recommended_action}")
        if overrides:
            print(f"  guardrails: {overrides}")
        print(f"  final: {d2.issue_type} -> {d2.recommended_action}  {'✓' if ok else '✗'}")

print("\nSample policy search ('address change in transit', carrier=bluedart):")
for h in read.search_policy("address change in transit", carrier="bluedart")["results"]:
    print(f"  {h['chunk_id']}  score={h['score']}")

if hits:
    print(f"\nDiagnosis accuracy: {sum(hits.values())}/{len(hits)}")
