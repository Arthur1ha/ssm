"""Checkpointed state for one lamp conversation."""

from __future__ import annotations

from typing import Annotated, TypedDict

from langchain_core.messages import AnyMessage
from langgraph.graph.message import add_messages

from cloud.esp32.tool_registry import PHYSICAL_ACTIONS

PHYSICAL_TOOLS = {spec["tool"] for spec in PHYSICAL_ACTIONS.values()}
READ_ONLY_TOOLS = {"list_peers", "get_space_context"}


class LampState(TypedDict, total=False):
    messages: Annotated[list[AnyMessage], add_messages]
    goal: str
    run_id: str
    trace_id: str
    parent_run_id: str
    turns: int
    effects: int
    steps: list[dict]
    pending: dict | None
    status: str
    draft: str
    reply: str
    question: str
