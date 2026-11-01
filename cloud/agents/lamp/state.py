"""Checkpointed state for one lamp conversation."""

from __future__ import annotations

from typing import Annotated, TypedDict

from langchain_core.messages import AnyMessage
from langgraph.graph.message import add_messages

PHYSICAL_TOOLS = {"set_led_state", "set_led_color", "blink_led"}
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
