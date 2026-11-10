import pytest

from shipment_agent.carriers import CarrierAPIError, MockCarrierAdapter
from shipment_agent.carriers.status_map import UNMAPPED
from shipment_agent.schemas import NormalizedStatus as S


@pytest.mark.parametrize(
    "awb,carrier,status,past_edd",
    [
        ("DL1000000001", "delhivery", S.IN_TRANSIT, False),
        ("DL1000000002", "delhivery", S.IN_TRANSIT, True),
        ("BD2000000003", "bluedart", S.DELIVERY_FAILED, False),
        ("DL1000000004", "delhivery", S.DELIVERY_FAILED, False),
        ("DL1000000005", "delhivery", S.IN_TRANSIT, True),
        ("BD2000000006", "bluedart", S.DAMAGED, True),
        ("DL1000000007", "delhivery", S.DELIVERED, False),
        ("BD2000000008", "bluedart", S.UNKNOWN, True),
    ],
)
def test_normalized_status(awb, carrier, status, past_edd):
    t = MockCarrierAdapter(carrier).get_tracking(awb)
    assert t.current_status == status
    assert t.is_past_edd == past_edd
    assert t.events[0].event_time >= t.events[-1].event_time  # newest first


def test_stale_scan_hours():
    t = MockCarrierAdapter("delhivery").get_tracking("DL1000000005")
    assert t.hours_since_last_scan == 230


def test_unmapped_status_is_recorded():
    MockCarrierAdapter("bluedart").get_tracking("BD2000000008")
    assert ("bluedart", "UNDELIVERED - OFFICE CLOSED (SUNDAY)") in UNMAPPED


def test_attempt_count():
    assert MockCarrierAdapter("bluedart").get_tracking("BD2000000003").attempt_count == 1


def test_wrong_carrier_raises():
    with pytest.raises(KeyError):
        MockCarrierAdapter("bluedart").get_tracking("DL1000000001")


def test_flaky_carrier():
    c = MockCarrierAdapter("delhivery", failure_rate=1.0, seed=1)
    with pytest.raises(CarrierAPIError):
        c.get_tracking("DL1000000001")
