"""Seed scenarios: one shipment per kind of exception (plus two healthy ones).

Scan times are given as hours before "now", so the scenarios stay valid
whatever the clock says. Each scenario also carries the expected diagnosis,
which becomes the first evaluation dataset in Phase 5.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Scan:
    raw_status: str
    location: str
    hours_ago: float
    remarks: str | None = None


@dataclass
class Scenario:
    awb: str
    carrier: str
    order_id: str
    edd_days_from_now: float  # negative = already past
    scans: list[Scan]  # oldest first
    expected_issue: str | None  # None = no exception
    expected_action: str | None
    description: str = ""
    order: dict = field(default_factory=dict)


def _order(order_id, awb, carrier, name, channel, phone, addr, items, mode="prepaid"):
    return dict(
        order_id=order_id,
        awb=awb,
        carrier=carrier,
        customer_ref=f"CUST-{order_id[-4:]}",
        customer_first_name=name,
        preferred_channel=channel,
        phone=phone,
        email=f"{name.lower()}@example.com",
        shipping_address=addr,
        items=items,
        payment_mode=mode,
    )


HYD = dict(line1="Flat 402, Lakeview Apts, Road 12", line2="Banjara Hills", city="Hyderabad", state="Telangana", pincode="500034")
BLR = dict(line1="221 MG Road", city="Bengaluru", state="Karnataka", pincode="560001")
MUM_BAD = dict(line1="Near temple", city="Mumbai", state="Maharashtra", pincode="400001")
DEL = dict(line1="B-14 Saket", city="New Delhi", state="Delhi", pincode="110017")
PUN = dict(line1="12 FC Road", city="Pune", state="Maharashtra", pincode="411004")
CHN = dict(line1="7 Anna Salai", city="Chennai", state="Tamil Nadu", pincode="600002")

SCENARIOS: list[Scenario] = [
    Scenario(
        awb="DL1000000001", carrier="delhivery", order_id="ORD-0001",
        edd_days_from_now=2,
        scans=[Scan("Picked Up", "Gurgaon Hub", 30), Scan("In Transit", "Nagpur Hub", 6)],
        expected_issue=None, expected_action=None,
        description="Healthy, moving normally.",
        order=_order("ORD-0001", "DL1000000001", "delhivery", "Ananya", "whatsapp", "9876543210", HYD,
                     [dict(sku="SH-RUN-01", name="Running shoes", qty=1, unit_price=3499)]),
    ),
    Scenario(
        awb="DL1000000002", carrier="delhivery", order_id="ORD-0002",
        edd_days_from_now=-1,
        scans=[Scan("Picked Up", "Gurgaon Hub", 120), Scan("In Transit", "Bhopal Hub", 60)],
        expected_issue="delayed", expected_action="notify_delay",
        description="No scan for 60h and past EDD, but not yet long enough to suspect loss.",
        order=_order("ORD-0002", "DL1000000002", "delhivery", "Rahul", "sms", "9811122233", BLR,
                     [dict(sku="BK-001", name="Book set", qty=1, unit_price=899)]),
    ),
    Scenario(
        awb="BD2000000003", carrier="bluedart", order_id="ORD-0003",
        edd_days_from_now=0,
        scans=[
            Scan("SHIPMENT PICKED UP", "Delhi", 72),
            Scan("SHIPMENT ARRIVED", "New Delhi Saket", 20),
            Scan("SHIPMENT OUT FOR DELIVERY", "New Delhi Saket", 8),
            Scan("CONSIGNEE NOT AVAILABLE", "New Delhi Saket", 3, "Customer not reachable on phone. Door locked."),
        ],
        expected_issue="failed_attempt", expected_action="ask_reschedule",
        description="First delivery attempt failed, customer unavailable.",
        order=_order("ORD-0003", "BD2000000003", "bluedart", "Priya", "whatsapp", "9900011122", DEL,
                     [dict(sku="KT-KTL", name="Electric kettle", qty=1, unit_price=1599)], mode="cod"),
    ),
    Scenario(
        awb="DL1000000004", carrier="delhivery", order_id="ORD-0004",
        edd_days_from_now=0,
        scans=[
            Scan("Picked Up", "Surat Hub", 50),
            Scan("Reached Destination Hub", "Mumbai Fort", 18),
            Scan("Dispatched", "Mumbai Fort", 7),
            Scan("Address Incomplete", "Mumbai Fort", 4, "Address incomplete, no building name. Landmark only."),
        ],
        expected_issue="wrong_address", expected_action="ask_address",
        description="Delivery failed because the address is incomplete.",
        order=_order("ORD-0004", "DL1000000004", "delhivery", "Karan", "whatsapp", "9822233344", MUM_BAD,
                     [dict(sku="HD-01", name="Headphones", qty=1, unit_price=2499)]),
    ),
    Scenario(
        awb="DL1000000005", carrier="delhivery", order_id="ORD-0005",
        edd_days_from_now=-6,
        scans=[Scan("Picked Up", "Gurgaon Hub", 260), Scan("In Transit", "Jaipur Hub", 230)],
        expected_issue="lost", expected_action="carrier_inquiry",
        description="No scan for ~9.5 days. Suspected lost; must raise inquiry before refunding.",
        order=_order("ORD-0005", "DL1000000005", "delhivery", "Meera", "email", "9833344455", PUN,
                     [dict(sku="PH-01", name="Smartphone", qty=1, unit_price=12999)]),
    ),
    Scenario(
        awb="BD2000000006", carrier="bluedart", order_id="ORD-0006",
        edd_days_from_now=-1,
        scans=[
            Scan("SHIPMENT PICKED UP", "Bengaluru", 90),
            Scan("SHIPMENT ARRIVED", "Chennai", 40),
            Scan("SHIPMENT FOUND DAMAGED", "Chennai", 30, "Outer box crushed, contents broken. Shipment held."),
        ],
        expected_issue="damaged", expected_action="propose_reship",
        description="Damaged in transit at the destination hub.",
        order=_order("ORD-0006", "BD2000000006", "bluedart", "Arjun", "sms", "9844455566", CHN,
                     [dict(sku="LM-01", name="Table lamp", qty=1, unit_price=1899)]),
    ),
    Scenario(
        awb="DL1000000007", carrier="delhivery", order_id="ORD-0007",
        edd_days_from_now=-1,
        scans=[Scan("Picked Up", "Gurgaon Hub", 70), Scan("Dispatched", "Hyderabad", 30), Scan("Delivered", "Hyderabad", 26)],
        expected_issue=None, expected_action="close_resolved",
        description="Delivered. A stale case for this shipment should close.",
        order=_order("ORD-0007", "DL1000000007", "delhivery", "Sneha", "whatsapp", "9855566677", HYD,
                     [dict(sku="BT-01", name="Water bottle", qty=2, unit_price=499)]),
    ),
    Scenario(
        awb="BD2000000008", carrier="bluedart", order_id="ORD-0008",
        edd_days_from_now=-2,
        scans=[Scan("SHIPMENT PICKED UP", "Mumbai", 100), Scan("UNDELIVERED - OFFICE CLOSED (SUNDAY)", "Bengaluru", 50, "Office premises closed")],
        expected_issue="unclear", expected_action="escalate",
        description="Unmapped carrier status on a high-value order. Should escalate rather than guess.",
        order=_order("ORD-0008", "BD2000000008", "bluedart", "Vikram", "email", "9866677788", BLR,
                     [dict(sku="LP-01", name="Laptop", qty=1, unit_price=54999)]),
    ),
]

BY_AWB = {s.awb: s for s in SCENARIOS}
BY_ORDER = {s.order_id: s for s in SCENARIOS}
