"""Isolated LangGraph path for one lamp sensor or activity opportunity.

The production sensor listener still uses its existing path. This graph uses
the same gate and domain executor, but native tool calls instead of JSON text.
"""

from __future__ import annotations

import json
import time
import uuid
from typing import TypedDict

from langchain_core.messages import AIMessage, HumanMessage
from langgraph.graph import END, START, StateGraph

from cloud.agents.lamp.prompts import persona_reply_prompt, valid_lamp_reply
from cloud.agents.common.runner import GraphRun
from cloud.agents.common.replies import factual_fallback
from cloud.agents.lamp.tools.schemas import SCHEMAS
from cloud.observability import emit_agent_event

PHYSICAL = {"set_led_state", "set_led_color", "blink_led"}
VISIBLE = PHYSICAL | {"send_message", "request_peer_skill", "list_peers", "get_space_context"}


class ReactiveState(TypedDict, total=False):
    sense: dict
    run_id: str
    trace_id: str
    steps: list[dict]
    turns: int
    call: dict | None
    status: str
    reply: str
    autonomy_epoch: int


def _prompt(agent, state: ReactiveState) -> str:
    from cloud.agents.lamp.personality.profile import get_profile
    from cloud.personality.store import render_identity
    from cloud.personality.names import agent_names
    from cloud.space.context import get_space_context

    profile = get_profile()
    space = get_space_context(agent._find_led_unit(), state=agent._state)
    games = []
    if state["sense"].get("social_opportunity"):
        from cloud.peers.games import playable_games
        from cloud.peers.social import activity_peers
        activity, peers = activity_peers(agent._find_led_unit(), space["peers"])
        for peer in peers:
            games.extend({"target": peer["unit_id"], **item} for item in
                         playable_games((activity or {}).get("activity_id"),
                                        agent._find_led_unit(), peer["unit_id"]))
    if agent._belief_summary:
        history = f"历史规律摘要：{agent._belief_summary}"
    else:
        history = "\n".join(
            f"{time.strftime('%H:%M', time.localtime(item.get('ts', 0)))}: "
            f"{item.get('context', '')}（执行了：{'、'.join(item.get('actions', [])) or '无动作'}）"
            for item in agent._belief_history[-3:]
        )
    return (
        f"{render_identity(profile)}\n"
        "你负责桌面灯的自主判断。一次最多调用一个工具；看到真实结果后才决定下一步。"
        "无需动作时直接回答。accepted、executing、timeout 不表示完成。"
        "同伴消息投递不等于接受或物理完成；只给活动中的同伴发有用消息。"
        "在空间活动中，若当前可玩玩法非空且没有更高优先级事件，优先考虑用 game_invite 邀请同伴玩一局；"
        "如果本轮先调灯，完成后仍有可玩玩法与社交机会，应再决定是否发送邀请；"
        "仍可选择普通交流或不发消息，同伴也可拒绝。邀请不等于游戏完成。"
        "观测中的 teaching_candidates 是已校验且启用的主人教导。命中且当前可执行时，"
        "按其中的 agent_tool 和参数调用工具；若因安全或更高优先级要求不执行，应说明原因。"
        "仍须遵守用户让位和真实执行回执。"
        f"社交主动性：{profile['traits']['social_initiative']}/100；"
        f"调光敏感度：{profile['traits']['adjustment_sensitivity']}/100，"
        "它只影响重新评估机会，不直接映射颜色。\n"
        f"观测：{json.dumps(state['sense'], ensure_ascii=False, default=str)}\n"
        f"空间：{json.dumps(space, ensure_ascii=False, default=str)}\n"
        f"同伴名字与设备 ID：{json.dumps(agent_names(), ensure_ascii=False)}\n"
        f"当前可玩玩法：{json.dumps(games, ensure_ascii=False)}\n"
        f"{history}\n"
        f"已观察结果：{json.dumps(state.get('steps', []), ensure_ascii=False, default=str)}"
    )


def build_reactive_graph(agent, *, model=None, publish=None, checkpointer=None):
    """Compile one sensor-event graph; all device work stays in `_dispatch_tool`."""
    model = model or agent._llm
    from langchain_core.utils.function_calling import convert_to_openai_tool
    declarations = []
    for name in VISIBLE:
        schema = convert_to_openai_tool(SCHEMAS[name])
        schema["function"]["name"] = name
        declarations.append(schema)
    bound = model.bind_tools(declarations, parallel_tool_calls=False)
    if publish is None:
        from cloud.api.intents import publish_agent_event
        publish = publish_agent_event

    def event(state, kind, **fields):
        emit_agent_event(kind, agent_id=agent._find_led_unit(),
                         run_id=state.get("run_id"), trace_id=state.get("trace_id"),
                         origin_kind="agent_autonomous", **fields)

    def gate(state):
        if (state["autonomy_epoch"] != agent._autonomy_epoch
                or not agent._should_act() or not agent._device_online()):
            return {"status": "blocked", "call": None}
        return {"status": "executing"}

    def route_gate(state):
        return "plan" if state["status"] == "executing" else END

    def plan(state):
        if state.get("turns", 0) >= 4:
            return {"status": "blocked", "call": None}
        if (state["autonomy_epoch"] != agent._autonomy_epoch
                or not agent._should_act() or not agent._device_online()):
            return {"status": "blocked", "call": None}
        prompt = _prompt(agent, state)
        event(state, "model.request", stage="lamp_reactive", input=prompt)
        try:
            response = bound.invoke([HumanMessage(content=prompt)])
            if not isinstance(response, AIMessage):
                raise TypeError("invalid_model_message")
        except Exception as exc:
            event(state, "model.error", stage="lamp_reactive", error=type(exc).__name__)
            return {"status": "error", "call": None}
        calls = response.tool_calls or []
        event(state, "model.response", stage="lamp_reactive",
              output={"content": response.content, "tool_calls": calls})
        if response.invalid_tool_calls or len(calls) > 1:
            return {"status": "blocked", "call": None}
        if not calls:
            return {"status": "ok", "call": None}
        call = calls[0]
        if call.get("name") not in VISIBLE or not isinstance(call.get("args"), dict):
            return {"status": "rejected", "call": None}
        return {"turns": state.get("turns", 0) + 1,
                "call": {"name": call["name"], "args": call["args"],
                         "id": call.get("id") or "call_" + uuid.uuid4().hex}}

    def route_plan(state):
        return "act" if state.get("call") else "reply"

    def act(state):
        call = state["call"]
        if (state["autonomy_epoch"] != agent._autonomy_epoch
                or not agent._should_act() or not agent._device_online()):
            return {"status": "blocked", "call": None}
        name, args = call["name"], dict(call["args"])
        args.pop("taught_id", None)
        if name == "request_peer_skill":
            args["request_id"] = call["id"]
        event(state, "tool.dispatch", tool=name, params=args, tool_call_id=call["id"])
        started = time.monotonic()
        if name in PHYSICAL:
            from cloud.esp32.physical_tasks import stable_task_id
            task_id = stable_task_id(agent._find_led_unit(), state["run_id"], call["id"])
            ok = agent._dispatch_tool(name, args, agent._find_led_unit(), task_id=task_id)
        else:
            ok = agent._dispatch_tool(name, args, agent._find_led_unit())
        result = getattr(agent, "_last_reactive_tool_result", {"result": "unknown"})
        step = {"step_id": call["id"], "tool": name, "params": args, "result": result}
        event(state, "tool.result", tool=name, step_id=call["id"], result=result,
              status=result.get("result", "unknown"),
              duration_ms=round((time.monotonic() - started) * 1000))
        status = ("executing" if ok and result.get("result") == "ok"
                  else result.get("result", "error"))
        return {"steps": [*state.get("steps", []), step], "call": None,
                "status": status}

    def route_act(state):
        return "plan" if state["status"] == "executing" else "reply"

    def reply(state):
        physical = [s for s in state.get("steps", []) if s["tool"] in PHYSICAL]
        if (not physical or state["autonomy_epoch"] != agent._autonomy_epoch
                or not agent._should_act() or not agent._device_online()):
            return {"reply": ""}
        status = "ok" if all(s["result"].get("result") == "ok" for s in physical) else "partial"
        validation_status = state.get("status") if state.get("status") != "executing" else status
        from cloud.agents.common.autonomous_visibility import actionable_error
        from cloud.space.activity import get_activity_store
        activity = get_activity_store().current()
        unit_id = agent._find_led_unit()
        scope = (activity["activity_id"] if activity and
                 (activity.get("devices") or {}).get(unit_id, {}).get("owner") == activity["activity_id"]
                 else "lamp-reactive")
        completed = any((step.get("result") or {}).get("result") == "ok" for step in physical)
        error = next((code for step in reversed(physical)
                      if (code := actionable_error(step.get("result") or {}))), "")
        material_key = ("light", agent._stable_light_level or state["sense"].get("light_level"))
        for i, step in enumerate(physical):
            publish("agent_progress", agent_id=agent._find_led_unit(), scope="space",
                    space_id="main", source="autonomous", status=step["result"].get("result", "unknown"),
                    run_id=state["run_id"], step_id=step["step_id"], stage="observed", sequence=i)
        if not agent._reply_gate.should_reply(scope, completed=completed,
                                              material_key=material_key, error=error):
            return {"reply": ""}
        from cloud.agents.lamp.personality.profile import get_profile
        run = GraphRun(state["run_id"], "", "自主调光时观察到：" + json.dumps(
            state["sense"], ensure_ascii=False, default=str), time.time() + 15,
            steps=physical, status=validation_status,
            agent_id=agent._find_led_unit(), origin_kind="agent_autonomous")
        prompt = persona_reply_prompt(run, get_profile())
        try:
            answer = model.invoke([HumanMessage(content=prompt)]).content
            if not valid_lamp_reply(answer, run):
                raise ValueError("invalid_fact_claim")
        except Exception:
            answer = factual_fallback(agent._find_led_unit(), validation_status, run)
        if (state["autonomy_epoch"] == agent._autonomy_epoch
                and agent._should_act() and agent._device_online()
                and agent._reply_gate.mark_replied(scope, completed=completed,
                                                   material_key=material_key, error=error)):
            publish("agent_reply", agent_id=agent._find_led_unit(), scope="space",
                    space_id="main", source="autonomous", status=status,
                    run_id=state["run_id"], text=answer)
        return {"reply": answer}

    def remember(state):
        if state.get("status") in {"error", "blocked", "rejected"}:
            return {}
        # Beliefs are the same bounded process-local observations as the old
        # listener; the Graph run ID prevents duplicate append on node replay.
        history = agent._belief_history
        if not any(item.get("run_id") == state["run_id"] for item in history):
            sense = state["sense"]
            completed = [step["tool"] for step in state.get("steps", [])
                         if (step.get("result") or {}).get("result") == "ok"]
            history.append({"run_id": state["run_id"], "ts": time.time(),
                            "context": f"light={sense.get('light_level')} sound={sense.get('sound_detected')} led={sense.get('led_state')}",
                            "actions": completed})
            if len(history) > 10:
                history.pop(0)
            agent._beliefs_since_summary += 1
        if agent._beliefs_since_summary >= 5:
            recent = history[-5:]
            lines = [f"{time.strftime('%H:%M', time.localtime(item['ts']))}: "
                     f"{item['context']} → {'、'.join(item['actions']) or '无动作'}"
                     for item in recent]
            prompt = "以下是最近桌面空间状态记录，用一句话总结规律（20字以内）：\n" + "\n".join(lines)
            event(state, "model.request", stage="lamp_belief_summary", input=prompt)
            try:
                answer = model.invoke([HumanMessage(content=prompt)]).content
                agent._belief_summary = str(answer).strip()[:100]
                event(state, "model.response", stage="lamp_belief_summary",
                      output=agent._belief_summary)
            except Exception as exc:
                event(state, "model.error", stage="lamp_belief_summary",
                      error=type(exc).__name__)
                agent._belief_summary = ""
            agent._beliefs_since_summary = 0
        return {}

    graph = StateGraph(ReactiveState)
    for name, node in (("gate", gate), ("plan", plan), ("act", act),
                       ("reply", reply), ("remember", remember)):
        graph.add_node(name, node)
    graph.add_edge(START, "gate")
    graph.add_conditional_edges("gate", route_gate, {"plan": "plan", END: END})
    graph.add_conditional_edges("plan", route_plan, {"act": "act", "reply": "reply"})
    graph.add_conditional_edges("act", route_act, {"plan": "plan", "reply": "reply"})
    graph.add_edge("reply", "remember")
    graph.add_edge("remember", END)
    return graph.compile(checkpointer=checkpointer)


def run_reactive_event(agent, sense: dict, *, model=None, publish=None,
                       checkpointer=None, run_id: str | None = None) -> ReactiveState:
    """Invoke one isolated workflow with an event-specific thread ID."""
    run_id = run_id or "auto_" + uuid.uuid4().hex
    if checkpointer is None:
        from cloud.agents.common.checkpoint import FileCheckpointSaver
        checkpointer = FileCheckpointSaver()
    graph = build_reactive_graph(agent, model=model, publish=publish,
                                 checkpointer=checkpointer)
    return graph.invoke({"sense": sense, "run_id": run_id,
                         "trace_id": "tr_" + uuid.uuid4().hex,
                         "autonomy_epoch": agent._autonomy_epoch,
                         "steps": [], "turns": 0},
                        {"configurable": {"thread_id": f"lamp-reactive:{run_id}"}})


def prepare_sensor_event(agent, event: dict, *, now: float | None = None) -> tuple[dict, tuple] | None:
    """Apply the listener's debounce, opportunity and material-change gates."""
    from cloud.agents.lamp.personality.behavior import settled_light_level
    from cloud.agents.lamp.personality.profile import get_profile
    from cloud.esp32 import tools as lamp_tools
    from cloud.peers.social import activity_peers
    from cloud.space.context import get_space_context

    now = time.time() if now is None else now
    if not agent._should_act() or not agent._device_online():
        return None
    unit = str(event.get("unit_id") or "")
    last_events = getattr(agent, "_graph_last_event_ts", None)
    if last_events is None:
        last_events = agent._graph_last_event_ts = {}
    if unit and now - last_events.get(unit, 0) < 5:
        return None
    if unit:
        last_events[unit] = now
    sense = agent._sense()
    if sense is None:
        return None
    sense = dict(sense)
    observation = None
    payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
    if unit.endswith("_sound") and payload.get("detected") is True:
        observation = {"id": "sensor.sound_detected", "values": {"detected": True},
                       "source": unit, "ts": payload.get("ts", 0)}
    elif unit.endswith("_light") and "level" in payload:
        observation = {"id": "sensor.light_changed", "values": {"level": payload["level"]},
                       "source": unit, "ts": payload.get("ts", 0)}
    if observation:
        from cloud.agents.common import teachings
        from cloud.agents.common.teaching_bindings import candidates
        from cloud.cards.registry import get_registry
        card = get_registry().get_card(agent._find_led_unit())
        if card:
            sense["teaching_candidates"] = candidates(
                teachings.list_for(agent._find_led_unit()), observation, card, now=now)
            from cloud.esp32.tool_registry import TOOL_DECLARATIONS
            for candidate in sense["teaching_candidates"]:
                skill = next((item for item in card.get("skills", [])
                              if item.get("id") == candidate["binding"]["skill_id"]), None)
                action = (skill or {}).get("invoke", {}).get("action")
                candidate["agent_tool"] = next((name for name, declaration in TOOL_DECLARATIONS.items()
                                                if declaration.get("action") == action), None)
            sense["teaching_candidates"] = [candidate for candidate in sense["teaching_candidates"]
                                            if candidate["agent_tool"] in VISIBLE]
    profile = get_profile()
    stable_level = settled_light_level(
        agent, sense["light_level"], now,
        profile["traits"]["adjustment_sensitivity"])
    # A separate peer or teaching event can also wake the graph. Give it the
    # settled category so a transient level does not become a lighting goal.
    sense["light_level"] = stable_level
    sense["settled_light_level"] = stable_level
    sense["proactive_hints"] = agent._check_proactive(sense)
    space = get_space_context(agent._find_led_unit(), state=agent._state)
    activity, eligible = activity_peers(agent._find_led_unit(), space["peers"])
    opportunity = agent._social_state.opportunity(
        activity, eligible, space["recent_discoveries"],
        profile["traits"]["social_initiative"], unit_id=agent._find_led_unit())
    if opportunity:
        sense["social_opportunity"] = opportunity
    rule_sig = tuple(item["teaching_id"] for item in sense.get("teaching_candidates", ()))
    try:
        peers = lamp_tools.list_peers(agent._find_led_unit())
    except Exception:
        peers = []
    peer_sig = tuple((peer.get("unit_id"), peer.get("online"),
                      peer.get("device_connected"))
                     for peer in peers)
    signature = (
        profile["revision"], stable_level, sense["time_period"],
        rule_sig, peer_sig, tuple(sense["proactive_hints"]),
        (opportunity["kind"], tuple(opportunity["event_ids"]), int(now // 300))
        if opportunity else None,
    )
    if signature == agent._last_reason_sig or now - agent._last_reason_ts < 10:
        return None
    return sense, signature


def run_reactive_sensor_event(agent, event: dict, *, now: float | None = None,
                              **kwargs) -> ReactiveState | None:
    """One event-driven Graph run, ready for a future listener switch."""
    prepared = prepare_sensor_event(agent, event, now=now)
    if prepared is None:
        return None
    sense, signature = prepared
    agent._last_reason_ts = time.time() if now is None else now
    result = run_reactive_event(agent, sense, **kwargs)
    opportunity = sense.get("social_opportunity")
    if result.get("status") != "error":
        sent = any(step.get("tool") == "send_message" and
                   (step.get("result") or {}).get("result") == "accepted"
                   for step in result.get("steps") or [])
        if opportunity and sent:
            agent._social_state.evaluated(opportunity)
        # A physical action can finish this short Graph run before its social
        # choice. Keep the opportunity pending for the next sensor event.
        if not opportunity or sent or not result.get("steps"):
            agent._last_reason_sig = signature
    return result
