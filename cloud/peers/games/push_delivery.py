"""Commander/courier game: real search, lamp routing and observed leg nudges."""
from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import replace
from typing import TypedDict

from langgraph.graph import END, START, StateGraph

from cloud.agents.common.checkpoint import FileCheckpointSaver
from cloud.agents.common.runner import invoke_blocking
from .base import Game
from .delivery_decisions import decide
from .delivery_lifecycle import on_acceptance, on_invitation

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


class DeliveryStopped(Exception):
    pass


def _available(sender: str, target: str) -> bool:
    if {sender, target} != {LAMP, DOG}:
        return False
    from cloud.go2.tools.registry import get_tool_spec
    return get_tool_spec("go2_push_cargo") is not None


async def _execute(drive, event: dict, task_id: str) -> None:
    from cloud.api.intents import _dispatch_mqtt_task
    from cloud.cards.registry import get_registry
    from cloud.esp32.service import get_agent
    from cloud.esp32.tool_executor import execute_physical
    from cloud.go2.connection import go2
    from cloud.go2.control import control_lease
    from cloud.go2.navigation.navigator import navigator
    from cloud.go2.state import runtime_for
    from cloud.go2.tools.executor import execute_tool
    from cloud.space.activity import get_activity_store
    from cloud.space.context import get_space_context
    from cloud.space.decisions import resolve_decision
    from cloud.agents.go2.workflows.search import search_request
    from .base import activity_allows
    from .color_hunt import live_lamp_color

    messenger = event["messenger"]
    invitation_id = event.get("original_invitation_id") or event["message_id"]
    activity_id, deadline = event.get("activity_id"), event["deadline"]
    agent = get_agent()
    if agent is None or not _available(LAMP, DOG):
        messenger.send_result(event["message_id"], "blocked", task_id=task_id,
                              reason="delivery_service_unavailable")
        return
    context = replace(drive._context, expires_at=int(deadline), persist=False)
    owner = "delivery:" + invitation_id
    if not agent.begin_peer_game(owner):
        messenger.send_result(event["message_id"], "blocked", task_id=task_id, reason="lamp_busy")
        return
    epochs = agent._autonomy_epoch, agent._command_epoch
    odom_session = getattr(go2, "_odom_session_id", None)
    expected_color = None
    runtime = runtime_for(go2)
    runtime_epoch = runtime.data["epoch"] if runtime is not None else None
    goal = dict(drive._active_goal or {})
    goal.update(kind="game", game_id=GAME_ID)

    def check():
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
            if (not current or current["rgb"] != expected_color["rgb"]
                    or current["brightness"] != expected_color["brightness"]):
                raise DeliveryStopped("delivery_signal_changed_or_stale")

    def record(kind, text, *, role=DOG, **details):
        return get_activity_store().record_event(activity_id, kind, unit_id=role,
            text=str(text)[:1000], **{"game_id": GAME_ID, "trigger_message_id": invitation_id,
                                     "task_id": task_id, **details})

    def facts(state, role):
        return {"game": GAME.metadata(), "stage": state.get("stage", ""),
                "roles": {"lamp": "指挥官", "go2": "送货员"},
                "invitation": event["message"].get("content", ""),
                "space": get_space_context(LAMP if role == "lamp" else DOG, state=agent._state),
                "observation": state.get("observation"), "cargo": state.get("cargo"),
                "cargo_receipt": state.get("cargo_receipt"),
                "courier_report": state.get("courier_report"),
                "canonical_terms": state.get("order"),
                "destination_confirmed": expected_color is not None,
                "latest_push": state.get("push_receipt"), "sequence": state.get("sequence", 0)}

    async def decision(role, phase, state):
        check()
        return await decide(role, phase, facts(state, role),
            thread_id=f"delivery:{invitation_id}:{role}:{phase}:{state.get('stage', '')}:{state.get('sequence', 0)}",
            deadline=deadline)

    async def observe(question):
        check()
        receipt = await execute_tool("go2_observe", {"question": question}, context=context)
        text = str(receipt.get("message") or "").strip()
        if receipt.get("result") != "ok" or not text:
            raise DeliveryStopped("delivery_observation_unavailable")
        entry = record("observation", text, source="go2.camera")
        check()
        return {"text": text[:2000], "source": "go2.camera", "as_of": time.time(),
                "ref": (entry or {}).get("event_id")}

    async def physical(action, params, step, basis):
        check()
        physical_id = "task_" + uuid.uuid5(uuid.NAMESPACE_URL, owner + ":" + step).hex

        def dispatch(uid, cmd, arguments, ctx, _source, *, task_id=None):
            check()
            if uid != LAMP or cmd != action or arguments != params:
                raise PermissionError("delivery_signal_changed")
            return _dispatch_mqtt_task(uid, cmd, arguments,
                {"session_id": ctx.session_id, "actor": ctx.actor, "persist": False,
                 "expires_at": ctx.expires_at}, source="peer_autonomous", task_id=task_id)

        record("action", basis["reason"], role=LAMP, action=action,
               task_id=physical_id, decision_basis=basis, status="executing")
        result = await invoke_blocking(execute_physical, state=agent._state, unit_id=LAMP,
            action=action, params=params, context=context, dispatch=dispatch,
            source="peer_autonomous", task_id=physical_id, card=get_registry().get_card(LAMP),
            timeout=min(20, max(.1, deadline - time.time())))
        record("action_result", "目的地灯光动作已确认。" if result.get("result") == "ok"
               else "目的地灯光动作未确认。", role=LAMP, action=action,
               task_id=physical_id, decision_basis=basis, status=result.get("result", "unknown"))
        if result.get("result") != "ok":
            raise DeliveryStopped("delivery_signal_unconfirmed:" + str(result.get("error") or result.get("result")))
        check()

    async def roles(state):
        lamp, dog = await asyncio.gather(decision("lamp", "agree", {"stage": "roles"}),
                                         decision("go2", "agree", {"stage": "roles"}))
        for role, answer in ((LAMP, lamp), (DOG, dog)):
            record("message", answer["speech"], role=role)
        if not lamp["accepted"] or not dog["accepted"]:
            return {"result": "rejected", "reason": "delivery_roles_not_agreed"}
        return {"sequence": 0}

    async def cargo(state):
        observed = await observe("描述当前可辨认的地面货物及配送区域。请区分可移动物体、地面区域和普通杂物；"
                                 "没有看见就如实说明，不编造货物或站点。")
        answer = await decision("lamp", "cargo", {**state, "stage": "cargo", "observation": observed})
        record("message", answer["speech"], role=LAMP)
        if not answer["accepted"]:
            return {"result": "blocked", "reason": "cargo_not_specified", "observation": observed}
        return {"cargo": answer["cargo"], "observation": observed}

    async def find(state):
        check()
        search_id = "delivery_" + invitation_id + "_cargo"
        record("action", "按指挥官公布的特征寻找本单货物。", action="go2_find_object", task_id=search_id)
        with search_request(search_id, deadline):
            receipt = await execute_tool("go2_find_object", {"target": state["cargo"]}, context=context)
        record("action_result", receipt.get("message") or receipt.get("reason") or receipt.get("result"),
               action="go2_find_object", status=receipt.get("result", "unknown"),
               task_id=receipt.get("task_id") or search_id)
        if (receipt.get("result") != "ok" or receipt.get("outcome") != "nearby"
                or receipt.get("found") is not True or (receipt.get("stop") or {}).get("result") != "ok"
                or receipt.get("target") != state["cargo"] or receipt.get("task_id") != search_id):
            raise DeliveryStopped("cargo_search_unconfirmed:" + str(receipt.get("reason") or receipt.get("result")))
        check()
        return {"cargo_receipt": receipt}

    async def report(state):
        answer = await decision("go2", "next", {**state, "stage": "waiting_destination"})
        record("message", answer["speech"])
        if answer["action"] == "stop":
            return {"result": "cancelled", "reason": "courier_declined"}
        # No tool chosen by the courier can run before a real destination.
        return {"courier_report": answer}

    async def destination(state):
        observed = await observe("货物已按原任务找到。本单现在等待指挥官指定目的地。描述当前真正的配送区域"
                                 "（地面标记或区域）的颜色、边界和可辨认特征；不要把同色杂物当配送区域。"
                                 "若区域不可见或有多个相同区域，明确说明。")
        answer = await decision("lamp", "destination", {**state, "stage": "destination", "observation": observed})
        record("message", answer["speech"], role=LAMP)
        if not answer["accepted"]:
            return {"result": "blocked", "reason": "destination_not_specified", "observation": observed}
        if not observed.get("ref") or observed["ref"] not in answer["evidence_refs"]:
            raise DeliveryStopped("destination_current_observation_required")
        space = get_space_context(LAMP, state=agent._state)
        basis = resolve_decision(space, answer["speech"], answer["evidence_refs"], require_observation=True)
        return {"order": {"cargo": state["cargo"], "destination": answer["destination"],
                           "color": answer["color"], "color_name": answer["color_name"]},
                "decision_basis": basis, "observation": observed}

    async def confirm_order(state):
        lamp, dog = await asyncio.gather(decision("lamp", "agree", {**state, "stage": "order"}),
                                         decision("go2", "agree", {**state, "stage": "order"}))
        for role, answer in ((LAMP, lamp), (DOG, dog)):
            record("message", answer["speech"], role=role)
        if not lamp["accepted"] or not dog["accepted"]:
            return {"result": "rejected", "reason": "delivery_order_not_agreed"}
        return {}

    async def signal(state):
        nonlocal expected_color
        current = (agent._state.actuator_snapshot().get(LAMP, {}).get("state") or {})
        if current.get("ism") != "ON":
            await physical("SET_STATE", {"state": "ON"}, "signal:on", state["decision_basis"])
        await physical("SET_COLOR", state["order"]["color"], "signal:color", state["decision_basis"])
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
        check()
        answer = await decision("go2", "next", {**state, "stage": "delivery"})
        if answer.get("speech"):
            record("message", answer["speech"])
        action = answer["action"]
        if action == "finish":
            # A model's conclusion cannot create a delivery receipt.
            if not ((state.get("push_receipt") or {}).get("delivered") is True
                    and (state.get("push_receipt") or {}).get("result") == "ok"):
                return {"result": "blocked", "reason": "delivery_completion_unconfirmed"}
        elif action == "stop":
            return {"result": "blocked", "reason": "courier_stopped"}
        return {"next_action": answer}

    async def clarify(state):
        check()
        question = state["next_action"]
        if question.get("question"):
            record("message", question["question"])
        answer = await decision("lamp", "agree", {
            **state, "stage": "clarification", "courier_report": question})
        record("message", answer["speech"], role=LAMP)
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
        from cloud.go2.navigation.cargo_push import expect_cargo
        check()
        order, answer = state["order"], state["next_action"]
        push_id = "delivery_" + invitation_id + "_push_" + str(state["sequence"])
        record("action", answer["speech"], action="go2_push_cargo", task_id=push_id)
        reference = state.get("cargo_reference") or {
            "cargo": state["cargo"], "destination": order["destination"],
            "search_evidence": state["cargo_receipt"].get("evidence")}
        with expect_cargo(reference, request_id=push_id,
                          signal_rgb=expected_color["rgb"]), search_request(push_id, deadline):
            receipt = await execute_tool("go2_push_cargo", {"cargo": state["cargo"],
                "destination": order["destination"], "distance_m": answer["distance_m"]}, context=context)
        record("action_result", receipt.get("message") or receipt.get("reason") or receipt.get("result"),
               action="go2_push_cargo", status=receipt.get("result", "unknown"),
               task_id=receipt.get("task_id") or push_id)
        check()
        if receipt.get("result") != "ok":
            raise DeliveryStopped("cargo_push_unconfirmed:" + str(receipt.get("reason") or receipt.get("error") or receipt.get("result")))
        if (receipt.get("stop") or {}).get("result") != "ok":
            raise DeliveryStopped("cargo_push_stop_unconfirmed")
        if receipt.get("delivered") is True and receipt.get("confirmation") != "cargo_contained_in_verified_zone_and_stopped":
            raise DeliveryStopped("delivery_completion_unconfirmed")
        if not receipt.get("reference"):
            raise DeliveryStopped("cargo_identity_unconfirmed")
        return {"push_receipt": receipt, "cargo_reference": receipt.get("reference"),
                "sequence": state["sequence"] + 1}

    async def complete(state):
        receipt = state.get("push_receipt") or {}
        if (receipt.get("result") != "ok" or receipt.get("delivered") is not True
                or receipt.get("confirmation") != "cargo_contained_in_verified_zone_and_stopped"
                or (receipt.get("stop") or {}).get("result") != "ok"):
            raise DeliveryStopped("delivery_completion_unconfirmed")
        check()
        # Final wording comes from the courier after observing the real result.
        answer = await decision("go2", "next", {**state, "stage": "delivered"})
        record("message", answer["speech"])
        return {"result": "ok", "reason": "cargo_in_zone_confirmed"}

    def terminal_or(next_node):
        return lambda state: END if state.get("result") else next_node

    graph = StateGraph(DeliveryState)
    for name, node in (("roles", roles), ("cargo", cargo), ("find", find), ("report", report),
                       ("destination", destination), ("confirm_order", confirm_order),
                       ("signal", signal), ("plan", plan), ("observe", extra_observation),
                       ("clarify", clarify), ("push", push), ("complete", complete)):
        graph.add_node(name, node)
    graph.add_edge(START, "roles")
    for first, second in (("roles", "cargo"), ("cargo", "find"), ("find", "report"),
                           ("report", "destination"), ("destination", "confirm_order"),
                           ("confirm_order", "signal"), ("signal", "plan")):
        graph.add_conditional_edges(first, terminal_or(second))
    graph.add_conditional_edges("plan", lambda state: END if state.get("result") else
        "observe" if state["next_action"]["action"] == "observe" else
        "clarify" if state["next_action"]["action"] == "question" else
        "complete" if state["next_action"]["action"] == "finish" else "push")
    graph.add_edge("observe", "plan")
    graph.add_conditional_edges("clarify", terminal_or("plan"))
    graph.add_conditional_edges("push", lambda state: "complete" if state["push_receipt"].get("delivered") is True else "plan")
    graph.add_edge("complete", END)

    status, reason, work = "unknown", "delivery_unconfirmed", None
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
        saver, thread = FileCheckpointSaver(), "delivery:game:" + invitation_id
        async with saver.async_run_lock(thread):
            work = asyncio.create_task(graph.compile(checkpointer=saver).ainvoke({},
                {"configurable": {"thread_id": thread}, "recursion_limit": 1000}, durability="sync"))
            while not work.done():
                check()
                await asyncio.wait({work}, timeout=.2)
            final = await work
        status, reason = final.get("result", "unknown"), final.get("reason", "delivery_unconfirmed")
    except DeliveryStopped as exc:
        reason = str(exc)
        status = "unknown" if "unconfirmed" in reason else "cancelled"
    except Exception as exc:
        logger.exception("delivery failed invitation=%s", invitation_id)
        reason = "delivery_error:" + type(exc).__name__
    finally:
        try:
            if work is not None and not work.done():
                work.cancel()
                await asyncio.gather(work, return_exceptions=True)
            if status != "ok":
                lease = control_lease.snapshot()
                if lease is not None and lease.owner_session_id != context.session_id:
                    status, reason = "unknown", "control_lease_changed:" + reason
                else:
                    stopped = await asyncio.wait_for(navigator.stop_and_confirm(
                        owner_session_id=context.session_id), timeout=5)
                    if stopped.get("result") != "ok":
                        status, reason = "unknown", "stop_unconfirmed:" + reason
        except Exception as exc:
            logger.warning("delivery cleanup failed error=%s", type(exc).__name__)
            status, reason = "unknown", "cleanup_unconfirmed:" + reason
        finally:
            agent.end_peer_game(owner)
        lease = control_lease.snapshot()
        if (status == "unknown" and runtime is not None
                and runtime.data["epoch"] == runtime_epoch
                and runtime.data.get("exploration_owner") == context.session_id
                and (lease is None or lease.owner_session_id == context.session_id)):
            # An uncertain physical outcome cannot hand control straight
            # back to autonomous travel, even after releasing the game token.
            runtime.set_exploration(False)
        try:
            await drive._advance_task(goal, stage="finish", result={"result": status, "reason": reason})
        except Exception as exc:
            # Physical completion and lamp ownership cleanup precede the
            # journal; a journal failure must not prevent the original reply.
            logger.warning("delivery task journal unavailable error=%s", type(exc).__name__)
    if status != "ok":
        record("response", "本单配送已结束，尚未确认货物送达指定区域。", status=status, reason=reason)
    messenger.send_result(event["message_id"], status, task_id=task_id, reason=reason)


GAME = Game(
    game_id=GAME_ID,
    name="指挥官与送货员",
    description=("灯与狗任一方都可自主邀请对方，确认指挥官与送货员角色后公布货物特征；"
                 "狗先找货，找到后等待目的地指令，再按灯色匹配配送区域并推送货物。"
                 "狗以短距离行走尝试用腿推动货物，每步停稳、重新观察货物，入区复核后才算送达。"),
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
        "准备：不要求安装推板；允许机器狗在既有运动门禁和避障校验内以短距离行走尝试用腿推动地面货物。"
        "没有专用踢腿动作，不用挥手、伸展或跳跃冒充受控推送；具体货物是否能推动由真实实机反馈决定。"
        "场地布置可识别的货物及颜色不同的配送区域；货物需要狗根据特征寻找，不预先指定在狗面前。"
        "邀请与角色：灯与狗须处于同一个有效空间活动内，任一方均可结合当前任务和观察自主邀请对方；"
        "邀请及接受开局前须确认双方当前没有正在执行或等待确认的任务，任意一方忙碌时延后；空闲不等于愿意接受。"
        "不固定发起方，不用随机定时器强制邀请。对方自主决定是否接受，邀请送达不等于接受。"
        "接受后双方确认本局角色和职责：灯负责指挥与颜色信号，狗负责找货与配送；"
        "邀请方不默认成为指挥官，角色扮演不授予设备控制权限。"
        "公布货物：角色确认后，指挥官依据用户提供的信息或真实观察公布可辨认的货物特征；"
        "不固定货物、特征或描述句式，不编造尚未观察到的货物事实。"
        "找货：狗依据公布的特征自主观察、搜索并核对货物身份；特征不清或存在歧义时自主询问、复查。"
        "仅看见相似物或口头声称找到不算确认，须保留真实观察依据。"
        "等待指令：狗确认找到货物后报告结果并等待目的地指令，不能自行猜测或提前开始配送；"
        "等待期间可以自主问答、询问目的地或补充观察，不强制固定对白、问答轮次或等待时长。"
        "目的地：找到货物后，指挥官依据当前站点事实自主决定送往哪个区域，通过灯色给出指令；"
        "灯设置对应颜色并等待物理回执，狗将真实灯色与当前可识别的配送区域匹配，"
        "不把任意同色杂物当站点，也不凭历史摘要编造站点。指令或匹配不清时先澄清、观察，不盲推。"
        "配送：狗在目标明确且执行条件满足后，自主选择受控短距推送行动，每次0.1至0.25米，"
        "逐步靠近或尝试用腿推动，每步核验停稳、同一货物的真实位置及指定区域；"
        "机器人自身移动不等于货物移动，单纯画面框变化不算推送成功，不绕过原有避障或抹除货物障碍。"
        "货物偏离、卡住、丢失或通路受阻时停止，根据真实情况决定询问、复查或报告受阻，"
        "不凭移动距离猜测货物位置。"
        "送达：只有同一目标货物进入本单指定的配送区域、狗停稳已由真实反馈确认，"
        "且停稳后用新鲜画面复核货物入区、目标区域一致，才算送达；"
        "狗自身到站、普通导航成功、请求 accepted、模型描述或消息送达都不能替代。"
        "送达不等于签收，取件或签收由人完成并另行确认。"
        "中断：用户介入、活动结束、配送中目标灯色改变、设备断连、定位会话改变、观测过期或任务期限结束时停止并核验结果；"
        "停止未确认则报告结果未知，不宣称已停稳或送达。失败、超时按原任务查询，"
        "不改用普通导航冒充推货、不盲目重发动作，重启不重放旧配送。"
        "交流与结束：全过程的邀请、接受、角色确认、问答、回复、决策和具体行动由现有角色 Graph"
        "结合人格、当前观察、任务与真实结果自主判断；只约束上述先后依赖和完成判据，不预排对白与行动剧本。"
        "如实报告结果，后续交流、庆祝或再次提议由设备自主决定，并继续遵循当前活动、权限及真实反馈约束。"
    ),
    strategy=(
        "结合人格 traits、活动要求、货物与配送区域的新鲜观察决定是否游玩；"
        "可玩列表本身不构成邀请理由。任何一方都可主动邀请，角色与邀请方向独立。"
        "先明确货物再搜索，确认找到后等待配送指令，颜色用于匹配目的地而不是提前跳过找货。"
        "双方用自己的口吻交流，依据真实观察选择问题、回应、搜索、等待和配送行动；"
        "不预设货物特征、站点颜色顺序、固定延时、固定问答或成功剧情。"
        "模型决定目标、交流与下一步策略，具体推送、停止和送达判定由已有可信执行链及实机适配核验。"
        "货物、区域或接触路径不明确就复查或提问，不盲推；不能声称所有货物都可被腿推动。"
    ),
)
