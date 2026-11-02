"""Isolated Go2 autonomous action Graph; production mode callbacks are unchanged.

Only bounded scene facts enter state. Camera frames, keys and private memories
stay in their existing device stores. A future mode adapter must supply live
dispatch guards and the existing Go2 tool executor before enabling this Graph.
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from typing import Any, TypedDict

from langchain_core.messages import HumanMessage, SystemMessage
from langgraph.graph import END, START, StateGraph
from pydantic import BaseModel, Field

from cloud.agents.common.effects import FileEffectLedger
from cloud.agents.go2.personality.behavior import (
    decision_persona, reactive_action_candidates, reply_persona,
    social_action_candidates,
)
from cloud.go2.tools.tools import _VALID_SPORT_CMDS

AUTONOMY_COOLDOWN_S = 15
ACTION_COOLDOWN_S = 30
TURN_ACTIONS = frozenset({"turn_left", "turn_right"})


class ActionDecision(BaseModel):
    action: str | None = Field(description="One available posture or turn, or null to observe")
    reason: str = Field(description="A short explanation of this choice")


class AutonomousReply(BaseModel):
    text: str = Field(description="A brief first-person Go2 utterance grounded in the receipt")


class ActionState(TypedDict, total=False):
    run_id: str
    kind: str
    scene: str
    identity: str
    memory_summary: str
    space_summary: str
    active: bool
    connected: bool
    interrupted: bool
    now: float
    last_autonomous_ts: float
    action_cooldowns: dict[str, float]
    last_decision: dict[str, Any] | None
    candidate_actions: list[str]
    teaching_candidates: list[dict]
    decision: dict[str, Any]
    result: dict[str, Any]
    status: str
    response: str


def build_action_graph(
    model: Any,
    *,
    kind: str,
    profile_loader: Callable[[], dict],
    execute: Callable[[str, str], Awaitable[dict]],
    can_dispatch: Callable[[], bool],
    checkpointer: Any = None,
    effect_ledger: FileEffectLedger | None = None,
):
    """Compile one perception → decision → one action → factual reply flow.

    This graph never starts motion itself. `execute` must call the existing
    permission, FSM, map and lease guarded device executor.
    """
    if kind not in {"reactive", "social"}:
        raise ValueError("kind must be reactive or social")
    decision_model = model.with_structured_output(ActionDecision, method="function_calling")
    reply_model = model.with_structured_output(AutonomousReply, method="function_calling")
    ledger = effect_ledger or FileEffectLedger()

    async def gate(state: ActionState) -> ActionState:
        now = state.get("now", time.time())
        if not state.get("active") or not state.get("connected") or state.get("interrupted"):
            return {"status": "cancelled", "candidate_actions": []}
        if (kind == "reactive" and not state.get("teaching_candidates")
                and now - state.get("last_autonomous_ts", 0) < AUTONOMY_COOLDOWN_S):
            return {"status": "cooldown", "candidate_actions": []}
        profile = profile_loader()
        boldness = profile["traits"]["boldness"]
        candidates = (reactive_action_candidates(
            boldness, set(_VALID_SPORT_CMDS), set(TURN_ACTIONS))
            if kind == "reactive" else
            social_action_candidates(boldness, set(_VALID_SPORT_CMDS)))
        if kind == "reactive":
            taught = {item["binding"]["params"]["cmd"]
                      for item in state.get("teaching_candidates", [])
                      if item["binding"]["skill_id"] == "go2_sport" and
                      item["binding"]["params"].get("cmd") in _VALID_SPORT_CMDS}
            candidates = set(candidates) | taught
        available = [action for action in candidates
                     if now - state.get("action_cooldowns", {}).get(action, 0) >= ACTION_COOLDOWN_S]
        return {"status": "deciding", "candidate_actions": sorted(available)}

    async def decide(state: ActionState) -> ActionState:
        profile = profile_loader()  # latest edited profile on each decision
        prompt = (
            f"当前观察：{state.get('scene', '')}\n人物：{state.get('identity', '')}\n"
            f"空间：{state.get('space_summary', '')}\n记忆摘要：{state.get('memory_summary', '')}\n"
            f"已启用教导的观察命中：{state.get('teaching_candidates', [])}\n"
            f"可选动作：{', '.join(state['candidate_actions']) or '无'}。也可以只观察。"
            "若教导命中且其动作可选，应按教导行动；安全、用户中断和设备状态优先，"
            "不行动时在 reason 中说明原因。"
            "只做当前模式允许的姿态或转向，不走动；用户中断和设备安全优先。"
        )
        try:
            raw = await decision_model.ainvoke([
                SystemMessage(content=decision_persona(profile)), HumanMessage(content=prompt)])
            decision = ActionDecision.model_validate(raw)
        except Exception as exc:
            return {"status": "error", "decision": {"error": type(exc).__name__}}
        now = state.get("now", time.time())
        last = {"action": decision.action, "reason": decision.reason, "ts": now}
        if decision.action is None:
            return {"decision": last, "last_decision": last,
                    "last_autonomous_ts": now, "status": "observing"}
        if decision.action not in state["candidate_actions"]:
            return {"decision": last, "last_decision": last,
                    "last_autonomous_ts": now, "status": "rejected"}
        return {"decision": last, "last_decision": last,
                "last_autonomous_ts": now, "status": "selected"}

    async def dispatch(state: ActionState) -> ActionState:
        if not can_dispatch():
            return {"status": "cancelled", "result": {"result": "cancelled"}}
        run_id = state.get("run_id")
        if not isinstance(run_id, str) or not run_id:
            return {"status": "rejected", "result": {"result": "rejected",
                                                    "error": "missing_run_id"}}
        action = state["decision"]["action"]
        reason = state["decision"]["reason"]
        step_id = f"{kind}:action"
        try:
            reservation = ledger.reserve("go2", f"go2:{kind}:{run_id}", run_id,
                                         step_id, action, {"reason": reason})
        except ValueError:
            return {"status": "rejected", "result": {"result": "rejected",
                                                    "error": "effect_id_conflict"}}
        if reservation["reservation"] == "existing":
            result = reservation.get("result") or {
                "result": "unknown", "error": "prior_dispatch_unconfirmed"}
        else:
            try:
                result = await execute(action, reason)
            except Exception as exc:
                result = {"result": "unknown", "error": type(exc).__name__}
        if not isinstance(result, dict) or not isinstance(result.get("result"), str):
            result = {"result": "error", "error": "invalid_device_result"}
        if result["result"] not in {"accepted", "executing", "timeout", "unknown"}:
            ledger.mark_result("go2", f"go2:{kind}:{run_id}", run_id,
                               step_id, result)
        cooldowns = dict(state.get("action_cooldowns", {}))
        cooldowns[action] = state.get("now", time.time())
        return {"status": result["result"], "result": result,
                "action_cooldowns": cooldowns}

    async def narrate(state: ActionState) -> ActionState:
        result = state["result"]
        status = result["result"]
        if not can_dispatch():
            return {"status": "cancelled", "response": ""}
        facts = (f"动作 {state['decision']['action']}；原因 {state['decision']['reason']}；"
                 f"设备回执 {result}。只有 result=ok 才能说动作完成。")
        try:
            raw = await reply_model.ainvoke([
                SystemMessage(content=reply_persona(profile_loader())),
                HumanMessage(content=facts),
            ])
            answer = AutonomousReply.model_validate(raw).text.strip()
            if not answer or len(answer) > 4000:
                raise ValueError("invalid_reply")
            if status != "ok" and any(word in answer for word in ("完成了", "办好了", "成功了", "已完成")):
                raise ValueError("unconfirmed_completion_claim")
        except Exception:
            answer = ("汪，我还没收到动作完成的确认。" if status in
                      {"accepted", "executing", "timeout", "unknown"} else
                      "汪，这次动作没能完成。" if status != "ok" else
                      "汪，这件事办好了！")
        return {"response": answer}

    async def after_gate(state: ActionState) -> str:
        return "decide" if state.get("status") == "deciding" else END

    async def after_decide(state: ActionState) -> str:
        return "dispatch" if state.get("status") == "selected" else END

    async def after_dispatch(state: ActionState) -> str:
        return "narrate" if state.get("status") != "cancelled" else END

    graph = StateGraph(ActionState)
    graph.add_node("gate", gate)
    graph.add_node("decide", decide)
    graph.add_node("dispatch", dispatch)
    graph.add_node("narrate", narrate)
    graph.add_edge(START, "gate")
    graph.add_conditional_edges("gate", after_gate)
    graph.add_conditional_edges("decide", after_decide)
    graph.add_conditional_edges("dispatch", after_dispatch)
    graph.add_edge("narrate", END)
    return graph.compile(checkpointer=checkpointer)
