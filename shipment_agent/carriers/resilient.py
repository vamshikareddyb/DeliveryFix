"""Retries and rate limiting around any CarrierAdapter.

Reads (tracking, notes) are retried with exponential backoff and jitter on
CarrierAPIError. Actions (reattempt, address change, inquiry) are NOT retried
automatically: a timeout doesn't tell us whether the carrier acted, and a
blind retry could book two reattempts. A failed action comes back as
ActionResult(ok=False), and the graph escalates it to a person.

Every call, read or action, passes through a per-carrier token bucket so a
cycle over thousands of shipments stays inside the carrier's rate limit.
"""
from __future__ import annotations

import random
import threading
import time
from dataclasses import dataclass, field
from typing import Callable

from ..schemas import ActionResult, Address, CarrierNotes, DeliverySlot, TrackingResult
from .base import CarrierAdapter, CarrierAPIError


class TokenBucket:
    def __init__(self, rate_per_sec: float, burst: int,
                 now: Callable[[], float] = time.monotonic, sleep: Callable[[float], None] = time.sleep):
        self.rate, self.capacity = rate_per_sec, burst
        self.tokens = float(burst)
        self._now, self._sleep = now, sleep
        self._last = now()
        self._lock = threading.Lock()
        self.waited = 0.0  # total seconds spent waiting (for metrics/tests)

    def acquire(self) -> None:
        with self._lock:
            t = self._now()
            self.tokens = min(self.capacity, self.tokens + (t - self._last) * self.rate)
            self._last = t
            if self.tokens < 1:
                wait = (1 - self.tokens) / self.rate
                self._sleep(wait)
                self.waited += wait
                self.tokens = 0.0
                self._last = self._now()
            else:
                self.tokens -= 1


@dataclass
class RetryPolicy:
    max_attempts: int = 4
    base_delay: float = 0.5
    max_delay: float = 8.0
    jitter: float = 0.25  # ±25%


@dataclass
class CallStats:
    calls: int = 0
    retries: int = 0
    failures: int = 0
    action_failures: int = 0
    by_method: dict = field(default_factory=dict)


class ResilientCarrier:
    """Wraps a CarrierAdapter; exposes the same interface."""

    def __init__(self, inner: CarrierAdapter, retry: RetryPolicy | None = None, bucket: TokenBucket | None = None,
                 sleep: Callable[[float], None] = time.sleep, rng: random.Random | None = None):
        self.inner = inner
        self.name = inner.name
        self.retry = retry or RetryPolicy()
        self.bucket = bucket or TokenBucket(rate_per_sec=10, burst=20, sleep=sleep)
        self._sleep = sleep
        self._rng = rng or random.Random()
        self.stats = CallStats()

    def __getattr__(self, item):  # e.g. .calls on the mock, for tests
        return getattr(self.inner, item)

    def _count(self, method: str):
        self.stats.calls += 1
        self.stats.by_method[method] = self.stats.by_method.get(method, 0) + 1

    def _read(self, method: str, *args, **kwargs):
        p = self.retry
        for attempt in range(1, p.max_attempts + 1):
            self.bucket.acquire()
            self._count(method)
            try:
                return getattr(self.inner, method)(*args, **kwargs)
            except CarrierAPIError:
                if attempt == p.max_attempts:
                    self.stats.failures += 1
                    raise
                self.stats.retries += 1
                delay = min(p.max_delay, p.base_delay * 2 ** (attempt - 1))
                self._sleep(delay * (1 + self._rng.uniform(-p.jitter, p.jitter)))

    def _action(self, method: str, *args) -> ActionResult:
        self.bucket.acquire()
        self._count(method)
        try:
            return getattr(self.inner, method)(*args)
        except CarrierAPIError as e:
            self.stats.action_failures += 1
            return ActionResult(ok=False, action=method,
                                detail=f"Carrier unavailable ({e}); not retried automatically to avoid a duplicate request.")

    # ---- CarrierAdapter interface
    def get_tracking(self, awb: str, refresh: bool = False) -> TrackingResult:
        return self._read("get_tracking", awb, refresh=refresh)

    def get_notes(self, awb: str) -> CarrierNotes:
        return self._read("get_notes", awb)

    def request_reattempt(self, awb: str, slot: DeliverySlot) -> ActionResult:
        return self._action("request_reattempt", awb, slot)

    def update_address(self, awb: str, address: Address) -> ActionResult:
        return self._action("update_address", awb, address)

    def raise_inquiry(self, awb: str, issue: str) -> ActionResult:
        return self._action("raise_inquiry", awb, issue)


def resilient_carriers(carriers: dict, **kwargs) -> dict[str, ResilientCarrier]:
    return {name: ResilientCarrier(c, **kwargs) for name, c in carriers.items()}
