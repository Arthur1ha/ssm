"""Isolated Go2 peer-social decision Graph; not wired to production Drive."""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from typing import Any, Literal, TypedDict

from langchain_core.messages import HumanMessage, SystemMessage
from langgraph.graph import END, START, StateGraph
from pydantic import BaseModel, Field

from cloud.agents.common.effects import FileEffectLedger

class SocialDecision(BaseModel):
    decision: Literal["send_message", "skip"]
    target: str = ""
    content: str = Field(default="", max_length=1000)
    reason: str = ""


class SocialState(TypedDict, total=False):
    run_id: str
    activity_id: str
    activity: dict[str, Any]
    opportunity: dict[str, Any]
    peers: list[dict[str, Any]]
    space_summary: str
    active: bool
    connected: bool
    interrupted: bool
    pending_question: bool
    seen_event_ids: list[str]
    decision: dict[str, Any]
    delivery: dict[str, Any]
    status: str
    response: str


def build_social_graph(
    model: Any,
    *,
    profile_loader: Callable[[], dict],
    can_send: Callable[[str, str, str], bool],
    send_message: Callable[[str, str], Awaitable[dict]],
    checkpointer: Any = None,
    effect_ledger: FileEffectLedger | None = None,
):
    """Decide whether to message a peer, then recheck live budget and target."""
    structured_model = model.with_structured_output(SocialDecision, method="function_calling")
    ledger = effect_ledger or FileEffectLedger()

    async def gate(state: SocialState) -> SocialState:
        if (not state.get("active") or not state.get("connected")
                or state.get("interrupted") or state.get("pending_question")
                or not state.get("activity_id") or not state.get("peers")
                or not state.get("opportunity")):
            return {"status": "skipped"}
        event_ids = state.get("opportunity", {}).get("event_ids") or []
        if event_ids and all(event_id in state.get("seen_event_ids", []) for event_id in event_ids):
            return {"status": "skipped"}
        return {"status": "deciding"}

    async def decide(state: SocialState) -> SocialState:
        profile = profile_loader()
        targets = [{"unit_id": peer.get("unit_id"), "name": peer.get("name"),
                    "skills": (peer.get("skills") or [])[:5]} for peer in state["peers"]]
        prompt = (
            f"你的社交主动性：{profile['traits']['extraversion']}/100。"
            "只根据可靠发现判断是否要向同伴发一条有价值的短消息；"
            "发消息不代表对方答应或完成动作，也不要推断人物身份。\n"
            f"活动：{json.dumps(state.get('activity'), ensure_ascii=False)}\n"
            f"机会：{json.dumps(state['opportunity'], ensure_ascii=False)}\n"
            f"在线同伴：{json.dumps(targets, ensure_ascii=False)}\n"
            f"空间：{state.get('space_summary', '')}"
        )
        try:
            raw = await structured_model.ainvoke([
                SystemMessage(content=profile["prompt"]), HumanMessage(content=prompt)])
            decision = SocialDecision.model_validate(raw)
        except Exception as exc:
            return {"status": "error", "decision": {"error": type(exc).__name__}}
        seen = list(dict.fromkeys([*state.get("seen_event_ids", []),
                                  *(state["opportunity"].get("event_ids") or [])]))
        update: SocialState = {"decision": decision.model_dump(), "seen_event_ids": seen}
        if decision.decision == "skip":
            update["status"] = "skipped"
            return update
        allowed = {peer.get("unit_id") for peer in state["peers"]}
        if decision.target not in allowed or not decision.content.strip():
            update["status"] = "rejected"
            return update
        update["status"] = "selected"
        return update

    async def dispatch(state: SocialState) -> SocialState:
        decision = state["decision"]
        target = decision["target"]
        content = decision["content"].strip()
        reason = decision.get("reason") or content
        if not can_send(state["activity_id"], target, reason):
            return {"status": "blocked"}
        run_id = state.get("run_id")
        if not isinstance(run_id, str) or not run_id:
            return {"status": "rejected", "delivery": {"result": "rejected",
                                                       "error": "missing_run_id"}}
        try:
            reservation = ledger.reserve("go2", f"go2:social:{run_id}", run_id,
                                         "send_message", "send_message",
                                         {"target": target, "content": content})
        except ValueError:
            return {"status": "rejected", "delivery": {"result": "rejected",
                                                       "error": "effect_id_conflict"}}
        if reservation["reservation"] == "existing":
            outcome = reservation.get("result") or {
                "result": "unknown", "error": "prior_delivery_unconfirmed"}
        else:
            try:
                outcome = await send_message(target, content)
            except Exception as exc:
                outcome = {"result": "unknown", "error": type(exc).__name__}
        if not isinstance(outcome, dict):
            outcome = {"result": "error", "error": "invalid_delivery_result"}
        status = str(outcome.get("result") or "error")
        if status != "unknown":
            ledger.mark_result("go2", f"go2:social:{run_id}", run_id,
                               "send_message", outcome)
        return {"delivery": outcome, "status": status,
                "response": ("消息已发送，尚未确认同伴回复" if status == "accepted"
                             else "消息未成功投递")}

    async def after_gate(state: SocialState) -> str:
        return "decide" if state.get("status") == "deciding" else END

    async def after_decide(state: SocialState) -> str:
        return "dispatch" if state.get("status") == "selected" else END

    graph = StateGraph(SocialState)
    graph.add_node("gate", gate)
    graph.add_node("decide", decide)
    graph.add_node("dispatch", dispatch)
    graph.add_edge(START, "gate")
    graph.add_conditional_edges("gate", after_gate)
    graph.add_conditional_edges("decide", after_decide)
    graph.add_edge("dispatch", END)
    return graph.compile(checkpointer=checkpointer)
