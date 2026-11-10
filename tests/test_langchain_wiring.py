"""Checks the LangChain wrappers. Skipped when langchain_core isn't installed."""
import pytest

pytest.importorskip("langchain_core")

from langchain_core.language_models.fake_chat_models import GenericFakeChatModel  # noqa: E402
from langchain_core.messages import AIMessage  # noqa: E402

from shipment_agent.chains import run_investigation  # noqa: E402
from shipment_agent.tools import build_read_tools  # noqa: E402


def test_read_tools_are_structured_tools(ctx):
    tools = build_read_tools(ctx)
    assert {t.name for t in tools} == {
        "get_tracking_status", "get_carrier_notes", "get_order_details", "get_case_history", "search_policy"
    }
    out = next(t for t in tools if t.name == "get_order_details").invoke({"order_id": "ORD-0001"})
    assert out["customer_ref"] == "CUST-0001"


def test_no_action_tools_exposed(ctx):
    names = {t.name for t in build_read_tools(ctx)}
    assert not names & {"send_customer_message", "issue_refund", "create_reshipment", "request_rebooking"}


class _FakeToolModel(GenericFakeChatModel):
    def bind_tools(self, tools, **kw):
        return self


def test_investigation_loop_runs_tools(ctx):
    msgs = iter([
        AIMessage(content="", tool_calls=[{"name": "get_tracking_status", "id": "1",
                                            "args": {"awb": "DL1000000004", "carrier": "delhivery"}}]),
        AIMessage(content="Tracking: delivery failed, address incomplete."),
    ])
    report = run_investigation(_FakeToolModel(messages=msgs), build_read_tools(ctx),
                               "DL1000000004", "delhivery", "ORD-0004", "delivery attempt failed")
    assert report.tool_calls[0]["result"]["current_status"] == "delivery_failed"
    assert "address incomplete" in report.findings
