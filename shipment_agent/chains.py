"""LLM steps: investigator (tool-calling loop), diagnoser and reply parser.

Only langchain_core is used here, plus whichever chat model package you
choose (default: langchain_anthropic). The investigator is a plain tool loop
for now; in Phase 2 it becomes a LangGraph subgraph with the same tools.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field

from . import clock
from .config import SETTINGS
from .prompts import (
    DIAGNOSER_SYSTEM,
    DIAGNOSER_TASK,
    INVESTIGATOR_SYSTEM,
    INVESTIGATOR_TASK,
    REPLY_PARSER_SYSTEM,
)
from .schemas import CustomerReplyParse, Diagnosis

DEFAULT_MODEL = os.getenv("SHIPMENT_AGENT_MODEL", "claude-sonnet-5-5")


def get_llm(model: str = DEFAULT_MODEL, temperature: float = 0.0):
    """Default chat model. Pass any LangChain chat model to the functions below instead if you prefer."""
    from langchain_anthropic import ChatAnthropic

    return ChatAnthropic(model=model, temperature=temperature)


# --------------------------------------------------------------------------
# Investigator
# --------------------------------------------------------------------------
@dataclass
class InvestigationReport:
    findings: str
    tool_calls: list[dict] = field(default_factory=list)  # name, args, result (for traces / evals)


def run_investigation(llm, tools: list, awb: str, carrier: str, order_id: str, trigger: str,
                      max_steps: int = SETTINGS.max_investigation_steps) -> InvestigationReport:
    from langchain_core.messages import HumanMessage, SystemMessage, ToolMessage

    by_name = {t.name: t for t in tools}
    llm_with_tools = llm.bind_tools(tools)
    messages = [
        SystemMessage(INVESTIGATOR_SYSTEM),
        HumanMessage(INVESTIGATOR_TASK.format(awb=awb, carrier=carrier, order_id=order_id,
                                              trigger=trigger, now=clock.now().isoformat(timespec="minutes"))),
    ]
    log: list[dict] = []

    for _ in range(max_steps):
        ai = llm_with_tools.invoke(messages)
        messages.append(ai)
        if not ai.tool_calls:
            return InvestigationReport(findings=_text(ai), tool_calls=log)
        for call in ai.tool_calls:
            tool = by_name.get(call["name"])
            result = tool.invoke(call["args"]) if tool else {"error": f"unknown tool {call['name']}"}
            log.append({"name": call["name"], "args": call["args"], "result": result})
            messages.append(ToolMessage(content=json.dumps(result, default=str), tool_call_id=call["id"]))

    # Step budget used up: ask for the report with tools disabled.
    messages.append(HumanMessage("Step limit reached. Write the findings report now from what you have."))
    return InvestigationReport(findings=_text(llm.invoke(messages)), tool_calls=log)


def _text(msg) -> str:
    c = msg.content
    if isinstance(c, str):
        return c
    return "\n".join(part.get("text", "") for part in c if isinstance(part, dict))


# --------------------------------------------------------------------------
# Diagnoser
# --------------------------------------------------------------------------
def build_diagnoser(llm):
    from langchain_core.prompts import ChatPromptTemplate

    prompt = ChatPromptTemplate.from_messages([("system", DIAGNOSER_SYSTEM), ("human", DIAGNOSER_TASK)])
    return prompt | llm.with_structured_output(Diagnosis)


def diagnose(llm, report: InvestigationReport, awb: str, carrier: str, order_id: str, trigger: str) -> Diagnosis:
    return build_diagnoser(llm).invoke(
        dict(awb=awb, carrier=carrier, order_id=order_id, trigger=trigger, findings=report.findings)
    )


# --------------------------------------------------------------------------
# Customer reply parser
# --------------------------------------------------------------------------
def build_reply_parser(llm):
    from langchain_core.prompts import ChatPromptTemplate

    prompt = ChatPromptTemplate.from_messages([("system", REPLY_PARSER_SYSTEM), ("human", "{reply}")])
    return prompt | llm.with_structured_output(CustomerReplyParse)


def parse_reply(llm, reply: str, question: str) -> CustomerReplyParse:
    from zoneinfo import ZoneInfo

    today = clock.now().astimezone(ZoneInfo("Asia/Kolkata")).strftime("%A %Y-%m-%d")
    return build_reply_parser(llm).invoke(dict(reply=reply, question=question, today=today))
