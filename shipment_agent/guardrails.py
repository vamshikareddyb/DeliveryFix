"""Deterministic checks applied to every Diagnosis before the graph acts on it.

The LLM recommends; these rules have the final say. Pure Python so they are
easy to unit-test and never drift with a prompt change.
"""
from __future__ import annotations

from .config import SETTINGS, Settings
from .schemas import CaseHistory, Diagnosis, OrderDetails, TrackingResult

HIGH_VALUE_THRESHOLD = 25000.0


def apply_guardrails(
    d: Diagnosis,
    order: OrderDetails,
    tracking: TrackingResult | None,
    history: CaseHistory | None = None,
    settings: Settings = SETTINGS,
) -> tuple[Diagnosis, list[str]]:
    """Return a possibly-adjusted diagnosis and the list of overrides applied."""
    overrides: list[str] = []
    d = d.model_copy(deep=True)

    def override(action: str, why: str):
        if d.recommended_action != action:
            overrides.append(f"{d.recommended_action} -> {action}: {why}")
            d.recommended_action = action  # type: ignore[assignment]

    # 1. Low confidence or "unclear" always goes to a human.
    if d.issue_type == "unclear" or d.confidence < settings.min_diagnosis_confidence:
        override("escalate", f"confidence {d.confidence:.2f} / issue {d.issue_type}")

    # 2. High-value orders: only a plain delay notice is automated.
    elif order.order_value > HIGH_VALUE_THRESHOLD and d.recommended_action != "notify_delay":
        override("escalate", f"order value ₹{order.order_value:,.0f} above ₹{HIGH_VALUE_THRESHOLD:,.0f}")

    # 3. Never move money on a "lost" parcel until a carrier inquiry exists.
    elif d.issue_type == "lost" and d.recommended_action in ("propose_refund", "propose_reship"):
        inquiry_raised = bool(history and any(a.startswith("inquiry:") for a in history.actions_taken))
        if not inquiry_raised:
            override("carrier_inquiry", "no carrier inquiry raised yet")

    # 4. "lost" needs enough silence to be plausible.
    if (d.issue_type == "lost" and tracking is not None
            and tracking.hours_since_last_scan < settings.lost_suspect_days * 24):
        overrides.append(f"issue lost -> delayed: only {tracking.hours_since_last_scan:.0f}h since last scan")
        d.issue_type = "delayed"
        if d.recommended_action in ("carrier_inquiry", "propose_refund", "propose_reship"):
            d.recommended_action = "notify_delay"

    # 5. Refunds on undelivered COD orders make no sense (nothing was paid).
    if d.recommended_action == "propose_refund" and order.payment_mode == "cod":
        override("escalate", "refund proposed on a COD order")

    return d, overrides
