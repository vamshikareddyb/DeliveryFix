"""The graph's shape, defined once.

build.py turns this into a LangGraph StateGraph; simulate.py walks the same
definition in plain Python (tests, and environments without LangGraph).

    START -> investigate -> diagnose -> route by recommended_action
      notify_delay ------> schedule_recheck -> END   (status: monitoring)
      carrier_inquiry ---> schedule_recheck -> END   (status: monitoring)
      ask_reschedule ----------------------> END   (status: waiting_customer)
      ask_address -------------------------> END   (status: waiting_customer)
      propose_resolution ------------------> END   (status: awaiting_approval)
      escalate ----------------------------> END   (status: escalated)
      close_case --------------------------> END   (status: closed)

Rechecks: invoking the same thread again with trigger="recheck" re-enters at
investigate with the persisted state (recheck_count, history). That is the
"wait & recheck" loop; Phase 3 adds in-graph waits with interrupt().
"""

ENTRY = "investigate"

NODES = [
    "investigate",
    "diagnose",
    "notify_delay",
    "ask_reschedule",
    "ask_address",
    "carrier_inquiry",
    "propose_resolution",
    "escalate",
    "schedule_recheck",
    "close_case",
]

# Fixed edges. "END" marks the end of a run.
EDGES = [
    ("investigate", "diagnose"),
    ("notify_delay", "schedule_recheck"),
    ("carrier_inquiry", "schedule_recheck"),
    ("schedule_recheck", "END"),
    ("ask_reschedule", "END"),
    ("ask_address", "END"),
    ("propose_resolution", "END"),
    ("escalate", "END"),
    ("close_case", "END"),
]

# Conditional edge: after diagnose, the router (Nodes.route_after_diagnosis) picks one of these.
ROUTE_FROM = "diagnose"
ROUTE_TARGETS = [
    "notify_delay",
    "ask_reschedule",
    "ask_address",
    "carrier_inquiry",
    "propose_resolution",
    "escalate",
    "close_case",
]
