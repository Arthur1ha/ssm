"""Commander/courier game: real search, lamp routing and observed leg nudges."""
from __future__ import annotations

import asyncio
import logging
import time
import uuid
from contextlib import contextmanager
from dataclasses import replace
from typing import TypedDict

from langgraph.graph import END, START, StateGraph
from pydantic import BaseModel, ConfigDict, model_validator
from pydantic.json_schema import SkipJsonSchema

from cloud.agents.common.checkpoint import FileCheckpointSaver
from .base import Game
from .delivery_decisions import decide
from .delivery_lifecycle import on_acceptance, on_invitation
from .scope import begin_game_close, game_scope_available

GAME_ID = "push_delivery"
LAMP, DOG = "esp32_desk_led", "go2"
logger = logging.getLogger(__name__)


class DeliveryState(TypedDict, total=False):
    cargo: str
    cargo_receipt: dict
    observation: dict
    order: dict
    decision_basis: dict
    courier_report: dict
    next_action: dict
    push_receipt: dict
    cargo_reference: dict
    sequence: int
    result: str
    reason: str
    search_response: str
    push_response: str
    response_emotion: str


class DeliveryStopped(Exception):
    pass


def cargo_search_schema(cargo: str):
    """The Graph chooses when to search; the server owns this order's target.

    Inject before the common runner records/reserves the effect, so receipts,
    checkpoints and deduplication all use the original description.
    """
    class FindOrderCargo(BaseModel):
        """寻找并核对本单已指定的货物；目标由服务端绑定，无需重新填写。"""
        model_config = ConfigDict(extra="forbid")
        target: SkipJsonSchema[str] = cargo

        @model_validator(mode="before")
        @classmethod
        def bind_order_target(cls, values):
            return {**values, "target": cargo}

    return FindOrderCargo


def _push_completed(receipt):
    if receipt.get('result') != 'ok' or (receipt.get('stop') or {}).get('result') != 'ok':
        return False
    return (receipt.get('directional_push_attempted') is True
            and receipt.get('confirmation') == 'directional_move_and_stop'
            or receipt.get('delivered') is True
            and receipt.get('confirmation') == 'cargo_near_destination_and_stopped')


def _available(sender: str, target: str) -> bool:
    if {sender, target} != {LAMP, DOG}:
        return False
    from cloud.go2.tools.registry import get_tool_spec
    return get_tool_spec("go2_push_cargo") is not None


async def _execute(drive, event: dict, task_id: str) -> dict:
    from cloud.api.intents import _dispatch_mqtt_task
    from cloud.esp32.service import get_agent
    from cloud.go2.connection import go2
    from cloud.go2.control import control_lease
    from cloud.go2.navigation.navigator import navigator
    from cloud.go2.state import runtime_for
    from cloud.space.activity import get_activity_store
    from cloud.space.context import get_space_context
    from cloud.space.decisions import resolve_decision
    from cloud.agents.go2.workflows.search import search_request
    from .base import activity_allows
    from .color_hunt import live_lamp_color
    from .role_graph import run_device_task

    messenger = event["messenger"]
    invitation_id = event.get("original_invitation_id") or event["message_id"]
    activity_id, deadline = event.get("activity_id"), event["deadline"]
    agent = get_agent()
    if agent is None or not _available(LAMP, DOG):
        messenger.send_result(event["message_id"], "blocked", task_id=task_id,
                              reason="delivery_service_unavailable")
        return {"result": "blocked", "reason": "delivery_service_unavailable"}
    context = replace(drive._context, expires_at=int(deadline), persist=False)
    owner = "delivery:" + invitation_id
    if not agent.begin_peer_game(owner):
        messenger.send_result(event["message_id"], "blocked", task_id=task_id, reason="lamp_busy")
        return {"result": "blocked", "reason": "lamp_busy"}
    epochs = agent._autonomy_epoch, agent._command_epoch
    odom_session = getattr(go2, "_odom_session_id", None)
    expected_color = None
    stage = "preparing"
    runtime = runtime_for(go2)
    runtime_epoch = runtime.data["epoch"] if runtime is not None else None
    goal = dict(drive._active_goal or {})
    goal.update(kind="game", game_id=GAME_ID)

    def interruption_reason(fallback):
        activity = get_activity_store().get(activity_id) or {}
        return activity.get("end_reason") or (
            "user_intervention" if getattr(drive, "_user_handoff_in_progress", False) else
            "device_offline" if not go2.is_connected else fallback)

    def check():
        if not game_scope_available():
            raise DeliveryStopped("game_ended")
        if time.time() >= deadline:
            raise DeliveryStopped("game_expired")
        if (not activity_allows(activity_id, LAMP, DOG) or drive._activity_id() != activity_id):
            raise DeliveryStopped("activity_ended")
        if (drive.user_interrupt or not go2.is_connected
                or not agent._should_act() or not agent._device_online()
                or getattr(agent, "_peer_game_id", None) != owner
                or epochs != (agent._autonomy_epoch, agent._command_epoch)):
            raise DeliveryStopped("autonomy_interrupted_or_offline")
        if not odom_session or getattr(go2, "_odom_session_id", None) != odom_session:
            raise DeliveryStopped("odom_session_changed")
        lease = control_lease.snapshot()
        if lease is not None and lease.owner_session_id != context.session_id:
            raise DeliveryStopped("control_lease_changed")
        if expected_color is not None:
            current = live_lamp_color(LAMP)
            if not current:
                raise DeliveryStopped("delivery_signal_unconfirmed")
            if (current["rgb"] != expected_color["rgb"]
                    or current["brightness"] != expected_color["brightness"]):
                raise DeliveryStopped("delivery_signal_changed")

    event_counts = {}

    def record(kind, text, *, role=DOG, **details):
        if kind == "message" and not str(text or "").strip():
            return None
        identity = (kind, role, details.get("task_id", task_id), details.get("stage", ""), details.get("action", ""))
        ordinal = event_counts.get(identity, 0)
        event_counts[identity] = ordinal + 1
        source_id = "evt_" + uuid.uuid5(uuid.NAMESPACE_URL, repr((invitation_id, identity, ordinal))).hex
        fact_kind = {"action": "call", "action_progress": "call", "action_result": "result",
                     "observation": "observation"}.get(kind, "observation")
        return get_activity_store().record_event(activity_id, kind, unit_id=role,
            text=str(text)[:1000], **{"game_id": GAME_ID, "trigger_message_id": invitation_id,
                "task_id": task_id, "source_event_id": source_id,
                "fact_kind": fact_kind, "result_kind": "action",
                "stage": stage,
                "context_scope": "shared" if kind == "message" else "process", **details})

    def available():
        try:
            check()
        except DeliveryStopped:
            return False
        return True

    def enter_stage(value):
        nonlocal stage
        stage = value
        # Persist the stage before awaiting a model/device, including when
        # cancellation prevents that operation from producing a receipt.
        record("action_progress", "", display=False, status="executing")

    def observation_space(state, role):
        """The coordinator may explicitly supply this task's trusted camera result.

        Internal trace rows are not a global discovery feed. The lamp still
        needs the courier's requested, completed observation for this stage.
        """
        from cloud.space.decisions import decision_evidence

        space = get_space_context(LAMP if role == "lamp" else DOG, state=agent._state)
        observation = state.get("observation") or {}
        if observation.get("ref"):
            shared_observation = {"event_id": observation["ref"], "activity_id": activity_id,
                "event_type": "observation", "unit_id": DOG, "source_unit_id": DOG,
                "ts": observation["as_of"], "summary": observation["text"]}
            space = {**space, "recent_discoveries": [
                *(row for row in space.get("recent_discoveries", [])
                  if row.get("event_id") != observation["ref"]), shared_observation]}
            space["decision_evidence"] = decision_evidence(space)
        return space

    def facts(state, role, phase):
        data = {"game": GAME.metadata(), "stage": state.get("stage", ""),
                "roles": {"lamp": "指挥官", "go2": "送货员"},
                "invitation": event["message"].get("content", ""),
                "space": observation_space(state, role),
                "observation": state.get("observation"), "cargo": state.get("cargo"),
                "cargo_receipt": state.get("cargo_receipt"),
                "courier_report": state.get("courier_report"),
                "canonical_terms": state.get("order"),
                "destination_confirmed": expected_color is not None,
                "latest_push": state.get("push_receipt"), "sequence": state.get("sequence", 0)}
        if role == "go2" and phase == "next":
            from cloud.go2.tools.registry import get_tool_spec
            data["push_contract"] = get_tool_spec("go2_push_cargo").description
        return data

    async def decision(role, phase, state):
        check()
        observed_facts = facts(state, role, phase)
        answer = await decide(role, phase, observed_facts,
            thread_id=f"delivery:{invitation_id}:{role}:{phase}:{state.get('stage', '')}:{state.get('sequence', 0)}",
            deadline=deadline, availability=available)
        check()
        return {**answer, "_decision_space": observed_facts["space"]}

    def decision_basis(answer, role=DOG):
        from cloud.agents.common.progress_facts import resolve_public_decision

        if not answer.get("decision_reason"):
            return None
        try:
            return resolve_public_decision(answer.get("_decision_space") or
                get_space_context(role, state=agent._state),
                answer["decision_reason"], answer.get("evidence_refs") or [])
        except (ValueError, TypeError):
            # Display explanations cannot cancel a valid business action.
            # Mandatory destination evidence is checked independently below.
            logger.info("delivery optional decision explanation discarded")
            return None

    def task_receipt(result, tool):
        steps = result.get("tool_results") or result.get("steps") or []
        matching = [step.get("result") or {} for step in steps if step.get("tool") == tool]
        receipt = dict(matching[-1]) if matching else {"result": result.get("result", "unknown"),
            "reason": result.get("error") or "device_task_without_receipt"}
        if result.get("result") != "ok" and receipt.get("result") == "ok":
            receipt = {**receipt, "result": result.get("result", "unknown"),
                       "reason": result.get("error") or "device_task_unconfirmed"}
        return receipt

    async def observe(question):
        check()
        result = await run_device_task("go2", question, context=context,
            run_id="delivery_" + invitation_id + "_observe_" + uuid.uuid4().hex,
            deadline=deadline, allowed_tools=frozenset({"go2_observe"}),
            instructions="只观察本次要求的当前画面，不执行运动。工具失败时在本次任务内处理；最后返回实际观察。",
            result_transform=lambda _name, _args, receipt: {**receipt,
                "final": receipt.get("result") in {"ok", "cancelled"}},
            reply_required=False, availability=available, parent_run_id=invitation_id)
        receipt = task_receipt(result, "go2_observe")
        text = str(receipt.get("message") or "").strip()
        if receipt.get("result") != "ok" or not text:
            raise DeliveryStopped("delivery_observation_unavailable")
        entry = record("observation", text, source="go2.camera")
        check()
        return {"text": text[:2000], "source": "go2.camera", "as_of": time.time(),
                "ref": (entry or {}).get("event_id")}

    async def physical_signal(order, basis):
        check()
        def dispatch(uid, cmd, arguments, ctx, _source, *, task_id=None):
            check()
            if uid != LAMP or not (cmd == "SET_STATE" and arguments == {"state": "ON"}
                    or cmd == "SET_COLOR" and arguments == order["color"]):
                raise PermissionError("delivery_signal_changed")
            return _dispatch_mqtt_task(uid, cmd, arguments,
                {"session_id": ctx.session_id, "actor": ctx.actor, "persist": False,
                 "expires_at": ctx.expires_at}, source="peer_autonomous", task_id=task_id)

        def guard(name, arguments):
            clean = {key: value for key, value in arguments.items()
                     if value is not None and key not in {"decision_reason", "evidence_refs"}}
            if (name == "set_led_state" and clean == {"state": "ON"}
                    or name == "set_led_color" and clean == order["color"]):
                return None
            return {"result": "rejected", "error": "delivery_signal_changed"}

        result = await run_device_task("lamp", "显示本单已确认的目的地指令灯色。", context=context,
            run_id="delivery_" + invitation_id + "_signal", deadline=deadline,
            allowed_tools=frozenset({"set_led_state", "set_led_color"}),
            tool_guard=guard, dispatch=dispatch, task_context=lambda: {"order": order,
                "decision_basis": basis, "required_state": "ON"},
            instructions="仅将灯设置为ON和本单确切颜色/亮度；已经满足的状态不用重发。"
                         "开灯是前置动作，仍需继续设置颜色；错误在本任务内处理，不向同伴逐条报错。",
            result_transform=lambda _name, _args, receipt: {**receipt,
                "final": _name == "set_led_color" and receipt.get("result") in {"ok", "accepted"}},
            reply_required=False, availability=available, parent_run_id=invitation_id)
        if result.get("result") != "ok":
            raise DeliveryStopped("delivery_signal_unconfirmed:" + str(result.get("error") or result.get("result")))
        check()

    async def cargo(state):
        enter_stage("cargo")
        observed = await observe("为灯选择货物描述当前地面场景：分别描述有一定体积、可辨认厚度或高度的独立物品，"
                                 "以及地面色块、贴纸、小站点标记和普通杂物。提供物品相对狗的远近、外形、"
                                 "地面接触及周围通路等可见线索，帮助灯优先选择较近、容易接近和推动的货物；"
                                 "用途推测与实际外观分开，不把彩色标记直接归为货物，"
                                 "不凭画面断言重量或一定能推动，不编造距离、货物或站点。")
        answer = await decision("lamp", "cargo", {**state, "stage": "cargo", "observation": observed})
        record("message", answer["speech"], emotion=answer.get("emotion"), role=LAMP)
        if not answer["accepted"]:
            return {"result": "blocked", "reason": "cargo_not_specified", "observation": observed}
        return {"cargo": answer["cargo"], "observation": observed}

    async def find(state):
        enter_stage("find_cargo")
        check()
        search_id = "delivery_" + invitation_id + "_cargo"
        record("action", "", action="go2_find_object", arguments={"target": state["cargo"]}, task_id=search_id)

        def guard(name, arguments):
            if name != "go2_find_object" or arguments.get("target") == state["cargo"]:
                return None
            return {"result": "rejected", "error": "delivery_cargo_changed"}

        @contextmanager
        def tool_context(name, _arguments, step_id):
            with search_request(step_id, deadline, require_cargo_measurement=name == "go2_find_object"):
                yield

        def complete_find(name, _arguments, receipt):
            if name != "go2_find_object":
                return receipt
            complete = (receipt.get("result") == "ok" and receipt.get("outcome") == "nearby"
                        and receipt.get("found") is True and (receipt.get("stop") or {}).get("result") == "ok"
                        and receipt.get("target") == state["cargo"])
            return {**receipt, "final": complete or receipt.get("result") == "cancelled"}

        result = await run_device_task("go2", "寻找并核对本单货物：" + state["cargo"],
            context=context, run_id=search_id, deadline=deadline,
            allowed_tools=frozenset({"go2_observe", "go2_find_object"}),
            tool_declarations={"go2_find_object": cargo_search_schema(state["cargo"])},
            tool_guard=guard, tool_context=tool_context, result_transform=complete_find,
            task_context=lambda: {"cargo": state["cargo"], "stage": "find_cargo"},
            instructions="只寻找同一货物，找货目标由服务端绑定，不用重新填写target；可先观察，明确失败时在本子任务内修正。"
                         "找到且确认停稳后结束本子任务，报告货物真实特征并等待指挥官指定目的地，不能自行配送。",
            availability=available, parent_run_id=invitation_id)
        receipt = task_receipt(result, "go2_find_object")
        record("action_result", receipt.get("message") or "", public_result=receipt,
               action="go2_find_object", status=receipt.get("result", "unknown"),
               task_id=receipt.get("task_id") or search_id)
        if (receipt.get("result") != "ok" or receipt.get("outcome") != "nearby"
                or receipt.get("found") is not True or (receipt.get("stop") or {}).get("result") != "ok"
                or receipt.get("target") != state["cargo"]):
            raise DeliveryStopped("cargo_search_unconfirmed:" + str(receipt.get("reason") or receipt.get("result")))
        check()
        return {"cargo_receipt": receipt, "search_response": result.get("response") or result.get("reply") or "",
                "response_emotion": result.get("emotion", "neutral")}

    async def report(state):
        check()
        # Reuse the device brain's final response; do not ask a second model
        # to narrate the same successful find.
        record("message", state.get("search_response"), emotion=state.get("response_emotion"))
        return {"courier_report": {"result": "ok", "found": True,
                                  "receipt": state["cargo_receipt"]}}

    async def destination(state):
        enter_stage("destination")
        observed = await observe("货物已按原任务找到。本单现在等待指挥官指定目的地。描述当前真正的配送区域"
                                 "（色块、小地面标记或物体标识）的位置及可辨认特征；只需大概送到附近，"
                                 "不要求区域完整边界或能够容纳货物。若目标不可见或有多个相似目标，明确说明。")
        answer = await decision("lamp", "destination", {**state, "stage": "destination", "observation": observed})
        record("message", answer["speech"], emotion=answer.get("emotion"), role=LAMP)
        if not answer["accepted"]:
            return {"result": "blocked", "reason": "destination_not_specified", "observation": observed}
        if not observed.get("ref") or observed["ref"] not in answer["evidence_refs"]:
            raise DeliveryStopped("destination_current_observation_required")
        space = observation_space({**state, "observation": observed}, "lamp")
        basis = resolve_decision(space, answer.get("decision_reason") or
                                 f"选择{answer['destination']}作为本单目的地。",
                                 answer["evidence_refs"], require_observation=True,
                                 observed_space=answer["_decision_space"])
        return {"order": {"cargo": state["cargo"], "destination": answer["destination"],
                           "color": answer["color"], "color_name": answer["color_name"]},
                "decision_basis": basis, "observation": observed}

    async def signal(state):
        nonlocal expected_color
        enter_stage("signal")
        await physical_signal(state["order"], state["decision_basis"])
        color = state["order"]["color"]
        pending_color = {"rgb": tuple(color[key] for key in ("r", "g", "b")), "brightness": color["brightness"]}
        # MQTT state follows the result on some connections; allow only a
        # short projection delay, never another physical dispatch.
        until = min(deadline, time.time() + 3)
        while time.time() < until:
            live = live_lamp_color(LAMP)
            if live and live["rgb"] == pending_color["rgb"] and live["brightness"] == pending_color["brightness"]:
                # Only arm monitoring after the physical result has reached
                # the projection; the outer watcher also calls check().
                expected_color = pending_color
                check()
                return {}
            await asyncio.sleep(.1)
        raise DeliveryStopped("delivery_signal_unconfirmed")

    async def plan(state):
        enter_stage("delivery")
        check()
        answer = await decision("go2", "next", {**state, "stage": "delivery"})
        answer["decision_basis"] = decision_basis(answer)
        action = answer["action"]
        if action == "finish":
            if not _push_completed(state.get("push_receipt") or {}):
                return {"result": "blocked", "reason": "delivery_completion_unconfirmed"}
        elif action == "stop":
            return {"result": "blocked", "reason": "courier_stopped"}
        return {"next_action": answer}

    async def clarify(state):
        check()
        question = state["next_action"]
        if question.get("question"):
            record("message", question["question"], emotion=question.get("emotion"))
        answer = await decision("lamp", "agree", {
            **state, "stage": "clarification", "courier_report": question})
        record("message", answer.get("speech"), emotion=answer.get("emotion"), role=LAMP)
        if not answer["accepted"]:
            return {"result": "blocked", "reason": "courier_question_unresolved"}
        # The answer can explain this order, but cannot replace its cargo,
        # destination, signal, or previous physical evidence.
        return {"courier_report": question, "sequence": state["sequence"] + 1}

    async def extra_observation(state):
        answer = state["next_action"]
        observed = await observe(answer.get("question") or
            "核对本单同一货物、已约定的配送区域和当前画面，报告疑点。")
        return {"observation": observed, "sequence": state["sequence"] + 1}

    async def push(state):
        enter_stage("push")
        from cloud.go2.navigation.cargo_push import expect_cargo
        check()
        order, answer = state["order"], state["next_action"]
        push_id = "delivery_" + invitation_id + "_push_" + str(state["sequence"])
        params = {"cargo": state["cargo"], "destination": order["destination"]}
        if answer.get("distance_m") is not None:
            params["distance_m"] = answer["distance_m"]
        record("action", "", action="go2_push_cargo", arguments=params, task_id=push_id,
               decision_basis=answer.get("decision_basis"))
        reference = state.get("cargo_reference") or {
            "cargo": state["cargo"], "destination": order["destination"],
            "search_evidence": state["cargo_receipt"].get("evidence")}
        current_reference = reference

        def guard(name, arguments):
            if name != "go2_push_cargo":
                return None
            if (params.get("distance_m") is not None
                    and arguments.get("distance_m") != params["distance_m"]):
                return {"result": "rejected", "error": "cargo_delivery_contract_changed"}
            return None

        @contextmanager
        def tool_context(name, _arguments, step_id):
            with expect_cargo(current_reference, request_id=step_id,
                              signal_rgb=expected_color["rgb"]), search_request(step_id, deadline):
                yield

        def transform(name, _arguments, receipt):
            nonlocal current_reference
            if name != "go2_push_cargo":
                return receipt
            if receipt.get("result") == "ok" and receipt.get("reference"):
                current_reference = receipt["reference"]
            # Preserve each real receipt. A stopped positioning step is useful
            # progress, not completion of the courier's delivery subtask.
            return {**receipt, "final": _push_completed(receipt)
                    or receipt.get("result") == "cancelled"}

        result = await run_device_task("go2", f"将本单货物{state['cargo']}朝{order['destination']}尝试推送并确认停稳。",
            context=context, run_id=push_id, deadline=deadline,
            allowed_tools=frozenset({"go2_observe", "go2_push_cargo"}),
            tool_guard=guard, tool_context=tool_context, result_transform=transform,
            task_context=lambda: {"order": order, "parameters": params,
                "cargo_reference": current_reference, "stage": "delivery"},
            instructions="保留本单确切货物、目的地以及显式距离要求，不改订单。"
                         "必要的重新观察、站位和明确失败后的调整在本任务内解决，不逐条向同伴报错。"
                         "positioned/aligned/approached仍是前置进展；只有现有推物工具完成方向尝试并真实停稳，"
                         "或确认货物已在目的地附近且停稳，才结束本单；完成后不再推送或后退复查。"
                         "不能用未确认结果重发动作，不能声称未观测的货物位移或送达。",
            availability=available, parent_run_id=invitation_id)
        receipt = task_receipt(result, "go2_push_cargo")
        record("action_result", receipt.get("message") or "", public_result=receipt,
               action="go2_push_cargo", status=receipt.get("result", "unknown"),
               task_id=receipt.get("task_id") or push_id,
               decision_basis=answer.get("decision_basis"))
        check()
        if receipt.get("result") != "ok":
            raise DeliveryStopped("cargo_push_unconfirmed:" + str(receipt.get("reason") or receipt.get("error") or receipt.get("result")))
        if (receipt.get("stop") or {}).get("result") != "ok":
            raise DeliveryStopped("cargo_push_stop_unconfirmed")
        if receipt.get("delivered") is True and receipt.get("confirmation") != "cargo_near_destination_and_stopped":
            raise DeliveryStopped("delivery_completion_unconfirmed")
        if not receipt.get("reference"):
            raise DeliveryStopped("cargo_identity_unconfirmed")
        return {"push_receipt": receipt, "cargo_reference": receipt.get("reference"),
                "sequence": state["sequence"] + 1,
                "push_response": result.get("response") or result.get("reply") or "",
                "response_emotion": result.get("emotion", "neutral")}

    async def complete(state):
        receipt = state.get("push_receipt") or {}
        if not _push_completed(receipt):
            raise DeliveryStopped("delivery_completion_unconfirmed")
        check()
        # The courier's execution Graph already saved its result wording in
        # task_result. The common game boundary emits both roles' final lines.
        delivered = receipt.get('delivered') is True
        return {"result": "ok", "reason": "cargo_near_destination_confirmed" if delivered else "directional_push_attempt_completed"}

    def terminal_or(next_node):
        return lambda state: END if state.get("result") else next_node

    graph = StateGraph(DeliveryState)
    for name, node in (("cargo", cargo), ("find", find), ("report", report),
                       ("destination", destination),
                       ("signal", signal), ("plan", plan), ("observe", extra_observation),
                       ("clarify", clarify), ("push", push), ("complete", complete)):
        graph.add_node(name, node)
    # Invitation acceptance already agreed the game's fixed roles. The lamp
    # owns its chosen order; the courier brain checks it when planning/executing.
    # Reconfirming both roles and that same order adds no business evidence.
    graph.add_edge(START, "cargo")
    for first, second in (("cargo", "find"), ("find", "report"),
                           ("report", "destination"), ("destination", "signal"), ("signal", "plan")):
        graph.add_conditional_edges(first, terminal_or(second))
    graph.add_conditional_edges("plan", lambda state: END if state.get("result") else
        "observe" if state["next_action"]["action"] == "observe" else
        "clarify" if state["next_action"]["action"] == "question" else
        "complete" if state["next_action"]["action"] == "finish" else "push")
    graph.add_edge("observe", "plan")
    graph.add_conditional_edges("clarify", terminal_or("plan"))
    graph.add_conditional_edges("push", lambda state: "complete" if _push_completed(state["push_receipt"]) else "plan")
    graph.add_edge("complete", END)

    status, reason, work, final = "unknown", "delivery_unconfirmed", None, {}
    stop_reason = None
    try:
        check()
        if not event.get("game_preaccepted"):
            while True:
                check()
                accepted = messenger.get_status(event.get("acceptance_message_id")) or {}
                if accepted.get("state") == "ok":
                    break
                if accepted.get("state") in {"blocked", "rejected", "cancelled", "error", "unknown", "unanswered", "unconfirmed_start"}:
                    raise DeliveryStopped("acceptance_unconfirmed")
                await asyncio.sleep(.2)
        drive._active_goal = goal
        started = await drive._advance_task(goal, stage="start")
        if (started.get("result") or {}).get("result") == "blocked":
            raise DeliveryStopped("go2_activity_unavailable")
        record("game_started", "", status="executing")
        saver, thread = FileCheckpointSaver(), "delivery:game:" + invitation_id
        async with saver.async_run_lock(thread):
            work = asyncio.create_task(graph.compile(checkpointer=saver).ainvoke({"sequence": 0},
                {"configurable": {"thread_id": thread}, "recursion_limit": 1000}, durability="sync"))
            while not work.done():
                check()
                await asyncio.wait({work}, timeout=.2)
            final = await work
        status, reason = final.get("result", "unknown"), final.get("reason", "delivery_unconfirmed")
    except DeliveryStopped as exc:
        reason = str(exc)
        if reason in {"game_ended", "activity_ended", "autonomy_interrupted_or_offline"}:
            reason = interruption_reason(reason)
        status = "unknown" if "unconfirmed" in reason else "cancelled"
    except asyncio.CancelledError:
        status, reason = "cancelled", interruption_reason("autonomy_stopped")
        raise
    except Exception as exc:
        logger.exception("delivery failed invitation=%s", invitation_id)
        reason = "delivery_error:" + type(exc).__name__
    finally:
        begin_game_close()
        stop_confirmed = status == "ok"
        try:
            if work is not None and not work.done():
                work.cancel()
                await asyncio.gather(work, return_exceptions=True)
            if status != "ok":
                lease = control_lease.snapshot()
                if lease is not None and lease.owner_session_id != context.session_id:
                    status, stop_reason = "unknown", "control_lease_changed"
                else:
                    stopped = await asyncio.wait_for(navigator.stop_and_confirm(
                        owner_session_id=context.session_id), timeout=5)
                    stop_confirmed = stopped.get("result") == "ok"
                    if not stop_confirmed:
                        status, stop_reason = "unknown", stopped.get("reason") or "stop_unconfirmed"
        except Exception as exc:
            logger.warning("delivery cleanup failed error=%s", type(exc).__name__)
            status, stop_reason = "unknown", "cleanup_unconfirmed"
        finally:
            agent.end_peer_game(owner)
        lease = control_lease.snapshot()
        if (not stop_confirmed and runtime is not None
                and runtime.data["epoch"] == runtime_epoch
                and runtime.data.get("exploration_owner") == context.session_id
                and (lease is None or lease.owner_session_id == context.session_id)):
            # An uncertain physical outcome cannot hand control straight
            # back to autonomous travel, even after releasing the game token.
            runtime.set_exploration(False)
        try:
            await drive._advance_task(goal, stage="finish", result={"result": status, "reason": reason,
                "stage": stage, "physical_stop": "confirmed" if stop_confirmed else "unknown",
                "stop_reason": stop_reason})
        except Exception as exc:
            # Physical completion and lamp ownership cleanup precede the
            # journal; a journal failure must not prevent the original reply.
            logger.warning("delivery task journal unavailable error=%s", type(exc).__name__)
    if status != "ok":
        record("response", "本次配送已结束，尚未确认货物送达指定区域。", status=status, reason=reason)
    messenger.send_result(event["message_id"], status, task_id=task_id, reason=reason)
    outcome = {"result": status, "reason": reason, "stage": stage,
               "physical_stop": "confirmed" if stop_confirmed else "unknown", "stop_reason": stop_reason}
    if status == "ok" and _push_completed(final.get("push_receipt") or {}) and final.get("push_response"):
        outcome["role_replies"] = {DOG: {"speech": final["push_response"],
            "emotion": final.get("response_emotion", "neutral"), "status": status, "terminal": True}}
    return outcome


GAME = Game(
    game_id=GAME_ID,
    name="指挥官与送货员",
    description=("灯与狗任一方都可自主邀请对方，确认指挥官与送货员角色后公布货物特征；"
                 "灯负责选择货物、目的地和指令灯色，狗负责找货并朝目的地方向尝试推送，再报告真实结果。"),
    participants=("esp32_desk_led", "go2"),
    bidirectional=True,
    requires_idle=True,
    available=_available,
    execute=_execute,
    on_invitation=on_invitation,
    on_acceptance=on_acceptance,
    invitation_ttl_s=900,
    invitation="",
    acceptance="",
    rules=(
        "双方在同一活动中自主商量是否参与；接受后灯为指挥官、狗为送货员，邀请方向不改变分工。"
        "灯依据用户要求或真实观察公布一件可辨认货物，狗先寻找并核对它；"
        "确认找到后等待灯指定目的地和约定指令灯色，不提前猜测目的地。"
        "目的地可以是色块、小地面标记或物体，灯色用于本单指令，不要求与目的地外观同色。"
        "目标不清时可以询问或观察。狗使用已有推物工具尝试朝指定方向推送，不要求安装推板，"
        "不能凭外观保证物品能推动；具体动作和完成判据由工具核验。"
        "本单完成方向推送尝试并停稳即可结束；推送尝试、实测货物位移、大致送达及人工签收分别说明。"
        "只交流新约定、必要疑问、真实发现和结果，不预排对白，也不为每步动作复述执行流程。"
    ),
    strategy=(
        "结合人格 traits、活动要求、货物与配送区域的新鲜观察决定是否游玩；"
        "可玩列表本身不构成邀请理由。任何一方都可主动邀请，角色与邀请方向独立。"
        "先明确货物再搜索，确认找到后等待配送指令，颜色用于匹配目的地而不是提前跳过找货。"
        "灯自主决定货物，优先考虑较近、容易接近和推动且有一定体积的物品；站点标记留作目的地。"
        "双方用自己的口吻交流，依据真实观察选择问题、回应、搜索、等待和配送行动；"
        "不预设货物特征、站点颜色顺序、固定延时、固定问答或成功剧情。"
        "模型决定目标、交流与下一步策略，运动由设备工具负责。"
    ),
)
