"""Tracking database (SQLite here; Postgres in production, same tables).

    shipments  one row per shipment: latest normalized status, last scan, EDD
    events     every scan ever received, deduplicated on (shipment, status, time)
    cases      the case registry: one row per shipment that has an agent thread,
               mirroring the thread's status so the scheduler can query it cheaply

The detector and scheduler only read these tables. Nothing here calls an LLM.
"""
from __future__ import annotations

import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime
from typing import Iterable, Optional

from . import clock
from .schemas import NormalizedStatus, TrackingEvent

SCHEMA = """
CREATE TABLE IF NOT EXISTS shipments (
    shipment_id       TEXT PRIMARY KEY,   -- the AWB
    order_id          TEXT NOT NULL,
    carrier           TEXT NOT NULL,
    current_status    TEXT,
    last_scan_at      TEXT,
    expected_delivery TEXT,
    attempt_count     INTEGER DEFAULT 0,
    updated_at        TEXT
);
CREATE TABLE IF NOT EXISTS events (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    shipment_id       TEXT NOT NULL,
    raw_status        TEXT NOT NULL,
    normalized_status TEXT NOT NULL,
    location          TEXT,
    remarks           TEXT,
    event_time        TEXT NOT NULL,
    received_at       TEXT NOT NULL,
    source            TEXT NOT NULL,      -- 'webhook' | 'poll'
    UNIQUE (shipment_id, raw_status, event_time)
);
CREATE TABLE IF NOT EXISTS cases (
    shipment_id    TEXT PRIMARY KEY,
    status         TEXT NOT NULL,
    trigger        TEXT,
    opened_at      TEXT NOT NULL,
    updated_at     TEXT NOT NULL,
    next_check_at  TEXT,
    reply_deadline TEXT,
    closed_at      TEXT,
    runs           INTEGER DEFAULT 0,
    last_error     TEXT
);
CREATE INDEX IF NOT EXISTS idx_shipments_status ON shipments(current_status);
CREATE INDEX IF NOT EXISTS idx_cases_due ON cases(status, next_check_at);
"""

# Shipments in these states need no further tracking.
TERMINAL = (NormalizedStatus.DELIVERED.value, NormalizedStatus.RTO_INITIATED.value)
# Case statuses that mean "a thread already owns this shipment".
ACTIVE_CASE = ("open", "monitoring", "waiting_customer", "awaiting_approval", "escalated")


def iso(dt: datetime | str | None) -> Optional[str]:
    """Normalize every timestamp to one format so string comparison in SQL is safe."""
    if dt is None:
        return None
    if isinstance(dt, str):
        dt = datetime.fromisoformat(dt)
    return dt.isoformat(timespec="seconds")


@dataclass
class ShipmentRow:
    shipment_id: str
    order_id: str
    carrier: str
    current_status: Optional[str]
    last_scan_at: Optional[str]
    expected_delivery: Optional[str]
    attempt_count: int


@dataclass
class CaseRow:
    shipment_id: str
    status: str
    trigger: Optional[str]
    opened_at: str
    updated_at: str
    next_check_at: Optional[str]
    reply_deadline: Optional[str]
    closed_at: Optional[str]
    runs: int
    last_error: Optional[str]


class TrackingDB:
    def __init__(self, path: str = ":memory:"):
        self.path = path
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            self.conn.executescript(SCHEMA)

    def _exec(self, sql: str, args: Iterable = ()) -> sqlite3.Cursor:
        with self._lock:
            cur = self.conn.execute(sql, tuple(args))
            self.conn.commit()
            return cur

    def _all(self, sql: str, args: Iterable = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self.conn.execute(sql, tuple(args)).fetchall()

    # ------------------------------------------------------------------ shipments
    def register_shipment(self, shipment_id: str, order_id: str, carrier: str,
                          expected_delivery: datetime | None = None) -> None:
        self._exec(
            "INSERT INTO shipments (shipment_id, order_id, carrier, expected_delivery, updated_at) "
            "VALUES (?, ?, ?, ?, ?) ON CONFLICT(shipment_id) DO NOTHING",
            (shipment_id, order_id, carrier, iso(expected_delivery), iso(clock.now())),
        )

    def set_expected_delivery(self, shipment_id: str, edd: datetime) -> None:
        self._exec("UPDATE shipments SET expected_delivery = ? WHERE shipment_id = ?", (iso(edd), shipment_id))

    def shipment(self, shipment_id: str) -> Optional[ShipmentRow]:
        rows = self._all("SELECT * FROM shipments WHERE shipment_id = ?", (shipment_id,))
        return ShipmentRow(**{k: rows[0][k] for k in ShipmentRow.__annotations__}) if rows else None

    def active_shipments(self) -> list[ShipmentRow]:
        rows = self._all(
            f"SELECT * FROM shipments WHERE current_status IS NULL OR current_status NOT IN ({','.join('?' * len(TERMINAL))})",
            TERMINAL,
        )
        return [ShipmentRow(**{k: r[k] for k in ShipmentRow.__annotations__}) for r in rows]

    # ------------------------------------------------------------------ events
    def add_event(self, shipment_id: str, event: TrackingEvent, source: str) -> bool:
        """Store one scan. Returns False if it was a duplicate (same scan via webhook and poll)."""
        cur = self._exec(
            "INSERT OR IGNORE INTO events (shipment_id, raw_status, normalized_status, location, remarks, "
            "event_time, received_at, source) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (shipment_id, event.raw_status, event.normalized_status.value, event.location, event.remarks,
             iso(event.event_time), iso(clock.now()), source),
        )
        if cur.rowcount == 0:
            return False
        self._refresh_shipment(shipment_id)
        return True

    def _refresh_shipment(self, shipment_id: str) -> None:
        latest = self._all(
            "SELECT normalized_status, event_time FROM events WHERE shipment_id = ? "
            "ORDER BY event_time DESC, id DESC LIMIT 1", (shipment_id,))[0]
        attempts = self._all(
            "SELECT COUNT(*) AS n FROM events WHERE shipment_id = ? AND normalized_status = ?",
            (shipment_id, NormalizedStatus.DELIVERY_FAILED.value))[0]["n"]
        self._exec(
            "UPDATE shipments SET current_status = ?, last_scan_at = ?, attempt_count = ?, updated_at = ? "
            "WHERE shipment_id = ?",
            (latest["normalized_status"], latest["event_time"], attempts, iso(clock.now()), shipment_id),
        )

    def last_event_received(self, shipment_id: str) -> Optional[str]:
        rows = self._all("SELECT MAX(received_at) AS t FROM events WHERE shipment_id = ?", (shipment_id,))
        return rows[0]["t"] if rows else None

    def events(self, shipment_id: str) -> list[dict]:
        return [dict(r) for r in self._all(
            "SELECT * FROM events WHERE shipment_id = ? ORDER BY event_time", (shipment_id,))]

    # ------------------------------------------------------------------ cases
    def case(self, shipment_id: str) -> Optional[CaseRow]:
        rows = self._all("SELECT * FROM cases WHERE shipment_id = ?", (shipment_id,))
        return CaseRow(**dict(rows[0])) if rows else None

    def open_case(self, shipment_id: str, trigger: str) -> None:
        now = iso(clock.now())
        self._exec(
            "INSERT INTO cases (shipment_id, status, trigger, opened_at, updated_at, runs) VALUES (?, 'open', ?, ?, ?, 0) "
            "ON CONFLICT(shipment_id) DO UPDATE SET status='open', trigger=excluded.trigger, opened_at=excluded.opened_at, "
            "updated_at=excluded.updated_at, next_check_at=NULL, reply_deadline=NULL, closed_at=NULL, last_error=NULL",
            (shipment_id, trigger, now, now),
        )

    def sync_case(self, shipment_id: str, state: dict) -> None:
        """Mirror the thread's state into the registry after every run or resume."""
        status = state.get("status", "open")
        now = iso(clock.now())
        self._exec(
            "UPDATE cases SET status = ?, updated_at = ?, next_check_at = ?, reply_deadline = ?, "
            "closed_at = CASE WHEN ? = 'closed' THEN ? ELSE NULL END, runs = runs + 1, last_error = NULL "
            "WHERE shipment_id = ?",
            (status, now, iso(state.get("next_check_at")),
             iso(state.get("reply_deadline")) if status == "waiting_customer" else None,
             status, now, shipment_id),
        )

    def mark_error(self, shipment_id: str, error: str) -> None:
        self._exec("UPDATE cases SET status = 'error', last_error = ?, updated_at = ? WHERE shipment_id = ?",
                   (error[:500], iso(clock.now()), shipment_id))

    def cases(self, status: str | None = None) -> list[CaseRow]:
        rows = (self._all("SELECT * FROM cases WHERE status = ? ORDER BY shipment_id", (status,)) if status
                else self._all("SELECT * FROM cases ORDER BY shipment_id"))
        return [CaseRow(**dict(r)) for r in rows]

    def due_rechecks(self, now: datetime) -> list[str]:
        return [r["shipment_id"] for r in self._all(
            "SELECT shipment_id FROM cases WHERE status = 'monitoring' AND next_check_at <= ? ORDER BY next_check_at",
            (iso(now),))]

    def due_timeouts(self, now: datetime) -> list[str]:
        return [r["shipment_id"] for r in self._all(
            "SELECT shipment_id FROM cases WHERE status = 'waiting_customer' AND reply_deadline <= ? ORDER BY reply_deadline",
            (iso(now),))]

    def case_counts(self) -> dict[str, int]:
        return {r["status"]: r["n"] for r in self._all("SELECT status, COUNT(*) AS n FROM cases GROUP BY status")}
