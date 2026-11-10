"""The graph's shape, defined once.

build.py turns this into a LangGraph StateGraph; simulate.py walks the same
definition in plain Python (tests, and environments without LangGraph).

    START -> investigate -> diagnose -> route by recommended_action
      notify_delay -------> schedule_recheck -> END               monitoring
      carrier_inquiry ----> schedule_recheck -> END               monitoring
      ask_reschedule / ask_address -> wait_for_customer  ⏸ (interrupt)
          reply   -> parse_reply -> rebook | update_address -> schedule_recheck -> END
                                 -> escalate (upset, unclear, pickup, cancel, carrier refused)
          timeout -> send_reminder -> wait_for_customer ⏸        (first timeout)
                  -> handle_no_reply -> escalate (COD) | schedule_recheck (prepaid)
      propose_resolution -> await_approval ⏸ (interrupt)
          approved  -> execute_resolution -> close_case -> END
          rejected  -> escalate -> END
          invalid   -> await_approval ⏸ again (e.g. agent approving a manager-level refund)
      escalate -> END                                             escalated
      close_case -> END                                           closed

Rechecks: invoking the same thread again with trigger="recheck" re-enters at
investigate with the persisted state (recheck_count, history).
"""

ENTRY = "investigate"

NODES = [
    "investigate",
    "diagnose",
    "notify_delay",
    "carrier_inquiry",
    "ask_reschedule",
    "ask_address",
    "wait_for_customer",
    "send_reminder",
    "handle_no_reply",
    "parse_reply",
    "rebook",
    "update_address",
    "propose_resolution",
    "await_approval",
    "execute_resolution",
    "escalate",
    "schedule_recheck",
    "close_case",
]

# Nodes that pause the run with interrupt().
WAIT_NODES = {"wait_for_customer", "await_approval"}

# Fixed edges. "END" marks the end of a run.
EDGES = [
    ("investigate", "diagnose"),
    ("notify_delay", "schedule_recheck"),
    ("carrier_inquiry", "schedule_recheck"),
    ("ask_reschedule", "wait_for_customer"),
    ("ask_address", "wait_for_customer"),
    ("send_reminder", "wait_for_customer"),
    ("propose_resolution", "await_approval"),
    ("schedule_recheck", "END"),
    ("escalate", "END"),
    ("close_case", "END"),
]

# Conditional edges: source node -> (router method on Nodes, possible targets).
ROUTES = {
    "diagnose": ("route_after_diagnosis", ["notify_delay", "ask_reschedule", "ask_address", "carrier_inquiry",
                                           "propose_resolution", "escalate", "close_case"]),
    "wait_for_customer": ("route_after_wait", ["parse_reply", "send_reminder", "handle_no_reply"]),
    "handle_no_reply": ("route_after_no_reply", ["escalate", "schedule_recheck"]),
    "parse_reply": ("route_after_parse", ["rebook", "update_address", "escalate"]),
    "rebook": ("route_after_customer_action", ["schedule_recheck", "escalate"]),
    "update_address": ("route_after_customer_action", ["schedule_recheck", "escalate"]),
    "await_approval": ("route_after_approval", ["await_approval", "execute_resolution", "escalate"]),
    "execute_resolution": ("route_after_execution", ["close_case", "escalate"]),
}

# Every node must have exactly one way out.
assert set(NODES) == {src for src, _ in EDGES} | set(ROUTES), "topology: node without outgoing edge"
assert not ({src for src, _ in EDGES} & set(ROUTES)), "topology: node with both fixed and conditional edges"
