"""Hard business limits live in code, not in the policy documents.

RAG explains policy to the model; these values are what the graph actually enforces.
"""
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    # Detection thresholds (used by the Phase 4 scanner, and handy context now)
    stale_scan_hours: int = 48
    lost_suspect_days: int = 7

    # Money: anything above this needs a manager, not just a support agent
    refund_auto_approve_limit: float = 0.0  # 0 = every refund needs human approval
    refund_manager_limit: float = 5000.0
    refund_hard_cap: float = 50000.0

    # LLM behaviour
    min_diagnosis_confidence: float = 0.7  # below this the graph escalates
    max_investigation_steps: int = 6

    # Graph behaviour
    max_rechecks: int = 3  # after this many rechecks without resolution, escalate
    delay_recheck_hours: int = 24
    inquiry_recheck_hours: int = 72
    delay_eta_buffer_days: int = 2  # policy: add 2 days to EDD for surface shipments


SETTINGS = Settings()
