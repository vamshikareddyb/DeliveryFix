"""Carrier adapter interface.

The agent only ever talks to this interface. MockCarrierAdapter implements it
now; a ShiprocketAdapter or AfterShipAdapter can implement it later without
touching tools, chains or the graph.
"""
from __future__ import annotations

from typing import Protocol

from ..schemas import ActionResult, Address, CarrierNotes, DeliverySlot, TrackingResult


class CarrierAPIError(Exception):
    """Transient carrier failure (timeouts, 5xx). Safe to retry."""


class CarrierAdapter(Protocol):
    name: str

    def get_tracking(self, awb: str, refresh: bool = False) -> TrackingResult: ...

    def get_notes(self, awb: str) -> CarrierNotes: ...

    def request_reattempt(self, awb: str, slot: DeliverySlot) -> ActionResult: ...

    def update_address(self, awb: str, address: Address) -> ActionResult: ...

    def raise_inquiry(self, awb: str, issue: str) -> ActionResult: ...
