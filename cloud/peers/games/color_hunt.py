"""Find a matching object: rules, strategy, validation and execution."""
from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import replace

from .base import Game, activity_allows
from cloud.space.execution import ExecutionContext

logger = logging.getLogger(__name__)

def live_lamp_color(source: str) -> dict | None:
    from cloud.api.main import _esp32_state
    from cloud.space.context import get_space_context

    context = get_space_context("go2", state=_esp32_state)
    for entry in context["environment"]:
        if entry.get("source") != source or entry.get("status") != "current":
            continue
        values = entry.get("values") or {}
        rgb = tuple(values.get(key) for key in ("r", "g", "b"))
        if (all(type(value) is int and 0 <= value <= 255 for value in rgb)
                and type(values.get("brightness")) is int
                and values["brightness"] > 0):
            return {"rgb": rgb, "brightness": values["brightness"],
                    "as_of": entry["as_of"], "source": source}
    return None


def _color_hunt_available(sender: str, target: str) -> bool:
    return (sender, target) == ("esp32_desk_led", "go2")


def _color_name(rgb: tuple[int, int, int]) -> str | None:
    """Name only simple, visually distinguishable game colors."""
    r, g, b = rgb
    if min(rgb) >= 175 and max(rgb) - min(rgb) <= 75:
        return "白色"
    if max(rgb) <= 55:
        return None
    if r >= 120 and g >= 105 and b <= min(r, g) * 0.55:
        return "黄色"
    if r >= 110 and b >= 110 and g <= min(r, b) * 0.7:
        return "紫色"
    if g >= 110 and b >= 110 and r <= min(g, b) * 0.7:
        return "青色"
    dominant = max(range(3), key=rgb.__getitem__)
    if rgb[dominant] < 110 or rgb[dominant] - sorted(rgb)[1] < 65:
        return None
    return ("红色", "绿色", "蓝色")[dominant]


def valid_color_hunt_invitation(text: str) -> bool:
    """Keep every invite on the public rule while allowing the lamp's own voice."""
    return ("亮" in text and "颜色" in text and "找" in text
            and any(word in text for word in ("赢", "胜"))
            and "color_hunt" not in text and "机身" not in text)


async def _wait_for_lamp_color(event: dict, drive: object) -> tuple[dict | None, str]:
    """Wait for the lamp's edge-confirmed color result on Go2's acceptance."""
    messenger = event["messenger"]
    acceptance_id = event.get("acceptance_message_id")
    if not acceptance_id:
        return None, "acceptance_unconfirmed"
    deadline = min(event["deadline"], time.time() + 60)
    confirmed_at = None
    while time.time() < deadline:
        if (not activity_allows(event.get("activity_id"), event["message"]["sender"], "go2")
                or drive._activity_id() != event.get("activity_id")):
            return None, "activity_ended"
        if drive.user_interrupt:
            return None, "autonomy_interrupted"
        status = messenger.get_status(acceptance_id) or {}
        state = status.get("state")
        if state == "ok":
            confirmed_at = confirmed_at or time.time()
            color = live_lamp_color(str(event.get("source") or ""))
            if color and color["as_of"] >= int(event.get("acceptance_sent_at") or 0):
                return color, ""
            if time.time() - confirmed_at >= 3:
                return None, "lamp_color_unknown_or_stale"
        if state in {"blocked", "rejected", "error", "cancelled", "unknown",
                     "unanswered", "unconfirmed_start"}:
            return None, str((status.get("result") or {}).get("reason") or state)
        await asyncio.sleep(0.2)
    return None, "lamp_color_timeout"


async def _run_color_hunt(drive: object, event: dict, task_id: str) -> None:
    """Let Go2's existing graph choose observations and visual navigation."""
    from cloud.agents.go2.graph import run_go2_loop
    from cloud.go2.connection import go2
    from cloud.go2.navigation.navigator import navigator

    message, messenger = event["message"], event["messenger"]
    source = str(event.get("source") or "")
    activity_id = event.get("activity_id")
    if not activity_allows(activity_id, message["sender"], "go2"):
        messenger.send_result(event["message_id"], "cancelled", task_id=task_id,
                              reason="activity_ended")
        return
    color, color_error = await _wait_for_lamp_color(event, drive)
    if color is None:
        await drive._advance_task(drive._active_goal, stage="finish",
            result={"result": "rejected", "reason": color_error})
        messenger.send_result(event["message_id"], "rejected", task_id=task_id,
                              reason=color_error)
        messenger.send_message("go2", message["sender"],
            "这局还没拿到灯出题后的新鲜颜色，先不凭邀请文字猜。",
            reply_to=event["message_id"], reply_kind="reply")
        return
    try:
        snapshot, snapshot_error = navigator._snapshot()
    except Exception:
        snapshot, snapshot_error = None, "map_unavailable"
    if not go2.is_connected or snapshot is None:
        reason = "go2_disconnected" if not go2.is_connected else snapshot_error or "map_unavailable"
        await drive._advance_task(drive._active_goal, stage="finish",
                                  result={"result": "blocked", "reason": reason})
        messenger.send_result(event["message_id"], "blocked", task_id=task_id,
                              reason=reason)
        return
    color_name = _color_name(color["rgb"])
    if color_name is None:
        reason = "lamp_color_not_nameable"
        await drive._advance_task(drive._active_goal, stage="finish",
                                  result={"result": "blocked", "reason": reason})
        messenger.send_result(event["message_id"], "blocked", task_id=task_id,
                              reason=reason)
        return
    drive._active_goal = {**drive._active_goal, "kind": "game", "game_id": "color_hunt",
                          "target_color": color, "deadline": event["deadline"],
                          "stage": "search"}
    await drive._advance_task(drive._active_goal, stage="start")
    observed_rgb = None
    pending_observe_rgb = None
    announced_target = None
    observed_evidence = None
    candidate_basis = None

    def task_context() -> str:
        return json.dumps({"game_id": "color_hunt", "question_color": color_name,
                           "question_rgb": color["rgb"],
                           "lamp_color": live_lamp_color(source),
                           "latest_observation": observed_evidence,
                           "decision_guidance": "选择候选时用decision_reason第一人称简短说明刚才观察中的哪些事实支持这个选择，不复述规则，不声称已到达。",
                           "activity_id": activity_id}, ensure_ascii=False)

    def tool_guard(name: str, _params: dict) -> dict | None:
        nonlocal pending_observe_rgb, announced_target, candidate_basis
        if not activity_allows(activity_id, message["sender"], "go2"):
            return {"result": "cancelled", "error": "activity_ended"}
        if drive.user_interrupt or not go2.is_connected:
            return {"result": "cancelled", "error": "autonomy_interrupted_or_offline"}
        latest = live_lamp_color(source)
        if latest is None or latest["rgb"] != color["rgb"]:
            return {"result": "blocked", "error": "question_color_changed_before_arrival"}
        if name == "go2_observe":
            pending_observe_rgb = latest["rgb"]
        elif name == "go2_navigate_to_visible_target":
            if observed_rgb != color["rgb"] or observed_evidence is None:
                return {"result": "blocked", "error": "fresh_matching_observation_required"}
            target = str(_params.get("target") or "").strip()
            if color_name not in target and color_name[0] not in target:
                return {"result": "blocked", "error": "target_color_not_described"}
            reason = _params.get("decision_reason")
            if not isinstance(reason, str) or not reason.strip() or len(reason) > 300:
                return {"result": "rejected", "error": "decision_reason_required",
                        "message": "我还没说明这次为什么选择这个候选，先不开始导航。"}
            announced_target = target
            candidate_basis = {"reason": reason.strip(), "evidence": [observed_evidence]}
            drive._activity_event("message", reason.strip(),
                                  game_id="color_hunt", trigger_message_id=event["message_id"],
                                  decision_basis=candidate_basis)
        return None

    def on_step(step: dict) -> None:
        nonlocal observed_rgb, observed_evidence
        tool = step.get("tool")
        receipt = step.get("result") or {}
        if tool == "go2_observe":
            latest = live_lamp_color(source)
            observed_rgb = None
            observed_evidence = None
            observation_text = receipt.get("message")
            if (receipt.get("result") == "ok" and latest and latest["rgb"] == pending_observe_rgb
                    and isinstance(observation_text, str) and observation_text.strip()):
                observation = drive._activity_event("observation", observation_text[:500],
                    source="go2.camera", game_id="color_hunt", action=tool,
                    trigger_message_id=event["message_id"])
                if observation and observation.get("event_id"):
                    observed_rgb = pending_observe_rgb
                    observed_evidence = {"ref": observation["event_id"], "kind": "observation",
                        "source": "go2", "as_of": observation["ts"],
                        "summary": observation_text.strip()[:240]}
        drive._activity_event("action_result", str(receipt.get("message") or
                              receipt.get("error") or receipt.get("result"))[:500],
                              action=tool, task_id=task_id, game_id="color_hunt",
                              trigger_message_id=event["message_id"],
                              **({"decision_basis": candidate_basis}
                                 if tool == "go2_navigate_to_visible_target" and candidate_basis else {}),
                              status=receipt.get("result", "unknown"))

    goal = (
        f"来自灯 {source} 的玩法邀请：{message.get('content', '')[:500]}。"
        f"你已自主接受 color_hunt；本局已由真实回执确认出题颜色为{color_name}。"
        "先用 go2_observe 重新分析当前场景；依据真实观察自行选择清楚可辨认的同色静止候选；"
        f"自己选择 go2_navigate_to_visible_target，目标描述必须包含‘{color_name}’和具体物品名称。"
        "在导航的decision_reason中以第一人称自然说明：刚才观察到了什么，以及为什么选择这个同色候选；"
        "这是面向用户的简短发言，不复述内部字段或规则，不声称已经到达。"
        "视觉观察只能确认候选物可见，不能证明地图可达；"
        "无候选就如实结束；不要凭邀请、文字观察或模型回答宣称找到，"
        "不要使用盲走或坐标导航代替视觉目标导航。"
        "本局须导航确认停稳并重新观察同一目标，才算完成；缺少标定时如实报告发现但未到达。"
    )
    context = replace(drive._context,
                      expires_at=int(event["deadline"]))
    graph_task = asyncio.create_task(run_go2_loop(
        "peer_" + event["message_id"], goal, context=context,
        run_id=event["message_id"], deadline=event["deadline"],
        allowed_tools=frozenset({"go2_observe", "go2_navigate_to_visible_target",
                                 "get_space_context"}),
        tool_guard=tool_guard, task_context=task_context, on_step=on_step,
        publish_reply=False))
    graph_result = None
    stop_reason = None
    try:
        while not graph_task.done():
            if (not activity_allows(activity_id, message["sender"], "go2")
                    or drive._activity_id() != activity_id):
                stop_reason = "activity_ended"
            elif drive.user_interrupt or not go2.is_connected:
                stop_reason = "autonomy_interrupted_or_offline"
            elif time.time() >= event["deadline"]:
                stop_reason = "game_expired"
            if stop_reason:
                graph_task.cancel()
                await asyncio.gather(graph_task, return_exceptions=True)
                navigator.stop(owner_session_id=context.session_id)
                stopped = False
                if go2.is_connected:
                    try:
                        stopped = (await go2.send_command(
                            "StopMove", completion_timeout=5.0)).get("result") == "ok"
                    except Exception:
                        pass
                if not stopped:
                    stop_reason = "stop_unconfirmed"
                break
            await asyncio.sleep(0.2)
        if not stop_reason:
            graph_result = await graph_task
            if (not activity_allows(activity_id, message["sender"], "go2")
                    or drive._activity_id() != activity_id):
                stop_reason = "activity_ended"
            elif drive.user_interrupt or not go2.is_connected:
                stop_reason = "autonomy_interrupted_or_offline"
            elif time.time() >= event["deadline"]:
                stop_reason = "game_expired"
    except asyncio.CancelledError:
        graph_task.cancel()
        await asyncio.gather(graph_task, return_exceptions=True)
        raise
    except Exception as exc:
        logger.exception("color_hunt graph failed message=%s", event["message_id"])
        navigator.stop(owner_session_id=context.session_id)
        stopped = False
        if go2.is_connected:
            try:
                stopped = (await go2.send_command(
                    "StopMove", completion_timeout=5.0)).get("result") == "ok"
            except Exception:
                pass
        stop_reason = "game_graph_error" if stopped else "stop_unconfirmed"
        graph_result = {"result": "unknown", "error": type(exc).__name__}

    steps = (graph_result or {}).get("tool_results") or []
    visual = next((step.get("result") or {} for step in reversed(steps)
                   if step.get("tool") == "go2_navigate_to_visible_target"), None)
    reached_candidate = bool(announced_target and visual and visual.get("result") == "ok" and
                             visual.get("confirmation") ==
                             "odom_stopped_and_visual_recheck")
    if reached_candidate and not stop_reason:
        status, reason = "ok", "candidate_reached"
        reply = (str((graph_result or {}).get("response") or "").strip()[:1000]
                 or f"我已到达{announced_target}附近，停稳后重新确认了目标，这局找到了。")
    elif stop_reason:
        status = ("cancelled" if stop_reason in {"activity_ended", "game_expired",
                                                 "autonomy_interrupted_or_offline",
                                                 "activity_ended_or_interrupted"}
                  else "unknown")
        reason = stop_reason
        reply = ("这局已中断，停步结果还未确认。" if reason == "stop_unconfirmed"
                 else "这局已中断，已执行的动作请以活动记录和设备回执为准。")
    else:
        status = (graph_result or {}).get("result", "unknown")
        if status not in {"blocked", "rejected", "error", "cancelled"}:
            status = "unknown"
        last_receipt = (steps[-1].get("result") or {}) if steps else {}
        reason = (last_receipt.get("error") or last_receipt.get("reason") or
                  (graph_result or {}).get("error") or "no_confirmed_color_match")
        reply = (str(last_receipt.get("message"))[:160] + "；还不能确认找到与灯同色的物品。"
                 if status == "blocked" and last_receipt.get("message") else
                 "我检查了当前画面，但还不能确认找到与灯同色的物品。")
    await drive._advance_task(drive._active_goal, stage="finish", result={
        "result": status, "reason": reason,
        "target_color": color,
        "graph_run_id": (graph_result or {}).get("run_id"),
        "tool_results": steps})
    messenger.send_result(event["message_id"], status, task_id=task_id,
                          reason=reason)
    messenger.send_message("go2", message["sender"], reply,
                           reply_to=event["message_id"], reply_kind="reply")


def _handle_color_hunt_acceptance(messenger, message: dict, state) -> None:
    """Let the lamp choose a new color only after Go2 accepts its game invite."""
    from cloud.space.context import get_space_context
    from cloud.space.decisions import decision_evidence, resolve_decision
    from cloud.space.activity import get_activity_store

    original = messenger.get_status(message["reply_to"]) or {}
    activity_id = message.get("activity_id")
    if (original.get("interaction_kind") != "game_invite"
            or original.get("game_id") != "color_hunt"
            or original.get("sender") != "esp32_desk_led"
            or original.get("target") != "go2"
            or original.get("activity_id") != activity_id
            or original.get("state") not in {"waiting_start", "waiting_result"}
            or not activity_allows(activity_id, "esp32_desk_led", "go2")):
        messenger.send_result(message["message_id"], "rejected",
                              reason="invalid_game_acceptance")
        return
    from cloud.esp32.service import get_agent
    from cloud.api.intents import _dispatch_mqtt_task
    agent = get_agent()
    if agent is None or not agent._should_act() or not agent._device_online():
        messenger.send_result(message["message_id"], "blocked",
                              reason="lamp_autonomy_unavailable")
        return
    space = get_space_context("esp32_desk_led", state=agent._state)
    if not any(item.get("kind") == "observation" for item in decision_evidence(space)):
        messenger.send_result(message["message_id"], "blocked",
                              reason="game_observation_required")
        get_activity_store().record_event(activity_id, "message", unit_id="esp32_desk_led",
            target_unit_id="go2", text="目前还没有可参考的环境观察，先不凭空选颜色。",
            game_id="color_hunt", trigger_message_id=message["reply_to"])
        return
    context = ExecutionContext("peer_game_color_" + message["conversation_id"], "agent", False,
                               "main", "agent_autonomous", int(message["deadline"]))
    session = {"session_id": context.session_id, "actor": "agent",
               "persist": False, "expires_at": context.expires_at}
    dispatched_color = False
    dispatched_power = False

    def dispatch(unit_id, action, params, _context, _source, *, task_id=None):
        nonlocal dispatched_color, dispatched_power
        if (unit_id != "esp32_desk_led" or action not in {"SET_STATE", "SET_COLOR"}
                or not agent._should_act() or not agent._device_online()
                or not activity_allows(activity_id, "esp32_desk_led", "go2")
                or time.time() >= message["deadline"]):
            raise PermissionError("game_color_only")
        if action == "SET_STATE":
            if params.get("state") != "ON" or dispatched_power or dispatched_color:
                raise PermissionError("game_power_not_allowed")
            dispatched_power = True
        elif dispatched_color:
            raise PermissionError("game_color_already_chosen")
        else:
            rgb = tuple(params.get(key) for key in ("r", "g", "b"))
            if (not all(type(value) is int and 0 <= value <= 255 for value in rgb)
                    or _color_name(rgb) is None):
                raise PermissionError("game_color_not_nameable")
            dispatched_color = True
        task_id = _dispatch_mqtt_task(unit_id, action, params, session,
                                      source="peer_game_color", task_id=task_id)
        return task_id

    try:
        setup_started_at = time.time()
        setup_goal = (
            "Go2 已接受寻找同色物品。请依据当前共享上下文中的真实环境观察，"
            "自行选择观察中确实存在、清楚可辨认的物品颜色作为题目，不指定狗必须找哪个物品。"
            "用 set_led_color 设置所选颜色，并用 decision_reason 简短说明为何选择它，"
            "这句话会作为你自己的发言展示在动作前：按你的性格用第一人称自然说出打算与依据，"
            "把视觉发现归于实际观察者，不声称自己看到了同伴的画面；不写内部字段或工具名。"
            "evidence_refs 必须引用 space.decision_evidence 中相关 observation 的 ref。"
            "没有相关观察或观察不能支持所选颜色时，如实说明并结束，不猜颜色或编造物品。"
            "若灯尚未开启，先用 set_led_state 开灯并等待回执；开灯也必须附上本次选色的理由和观察引用。"
            "等待设备真实回执。只执行一次设色，不发送同伴消息，不预设之后变色或其他剧情。")
        result = agent.run_user_loop(
            setup_goal, "esp32_desk_led", context, dispatch,
            lighting_only=True, run_id=message["message_id"])
        if (result.get("result") == "error" and not result.get("steps")
                and not dispatched_power and not dispatched_color
                and time.time() + 10 < message["deadline"]):
            # A model connection error before the first tool has no physical
            # side effect; a fresh graph run may safely retry the decision.
            logger.warning("color hunt setup model failed before action; retrying id=%s",
                           message["message_id"])
            result = agent.run_user_loop(
                setup_goal, "esp32_desk_led", context, dispatch,
                lighting_only=True, run_id="retry_" + message["message_id"])
        color_step = next((step for step in reversed(result.get("steps") or [])
                           if step.get("tool") == "set_led_color"
                           and (step.get("result") or {}).get("result") == "ok"), None)
        params = (color_step or {}).get("params") or {}
        basis = (color_step or {}).get("result", {}).get("decision_basis") or {}
        if color_step:
            try:
                basis = resolve_decision(
                    get_space_context("esp32_desk_led", state=agent._state),
                    basis.get("reason"),
                    [item.get("ref") for item in basis.get("evidence", [])],
                    require_observation=True)
            except (ValueError, TypeError):
                messenger.send_result(message["message_id"], "rejected",
                                      reason="game_color_evidence_unconfirmed")
                return
        color = None
        if color_step:
            color_deadline = time.monotonic() + min(
                3, max(0, float(message["deadline"]) - time.time()))
            while time.monotonic() < color_deadline:
                color = live_lamp_color("esp32_desk_led")
                if (color and color["as_of"] >= int(setup_started_at)
                        and tuple(params.get(key) for key in ("r", "g", "b")) == color["rgb"]
                        and ("brightness" not in params
                             or params["brightness"] == color["brightness"])):
                    break
                time.sleep(0.1)
        if (not color_step or not color
                or dispatched_power and not any(
                    step.get("tool") == "set_led_state"
                    and (step.get("result") or {}).get("result") == "ok"
                    for step in result.get("steps") or [])
                or color["as_of"] < int(setup_started_at)
                or tuple(params.get(key) for key in ("r", "g", "b")) != color["rgb"]
                or ("brightness" in params and params["brightness"] != color["brightness"])):
            reason = "lamp_color_unknown_or_stale" if color_step else "lamp_color_not_confirmed"
            messenger.send_result(message["message_id"], "rejected", reason=reason)
            return
        if not activity_allows(activity_id, "esp32_desk_led", "go2"):
            messenger.send_result(message["message_id"], "cancelled", reason="activity_ended")
            return
        if not agent._should_act() or not agent._device_online():
            messenger.send_result(message["message_id"], "cancelled",
                                  reason="lamp_autonomy_unavailable")
            return
        color_task_id = (color_step.get("result") or {}).get("task_id")
        if color_task_id:
            messenger.send_progress(message["message_id"], color_task_id)
        messenger.send_result(message["message_id"], "ok", task_id=color_task_id)
        get_activity_store().record_event(activity_id, "action_result",
            unit_id="esp32_desk_led", text="游戏出题颜色已确认",
            action="set_led_color", status="ok", game_id="color_hunt",
            trigger_message_id=message["reply_to"], decision_basis=basis,
            task_id=(color_step.get("result") or {}).get("task_id"))
    except Exception as exc:
        logger.exception("color hunt setup failed id=%s", message["message_id"])
        messenger.send_result(message["message_id"], "error", reason=type(exc).__name__)


GAME = Game(
    game_id="color_hunt",
    name="寻找同色物品",
    description="灯亮一种颜色，狗在环境里找对应颜色的物品；找到了就算狗赢。",
    participants=("esp32_desk_led", "go2"),
    available=_color_hunt_available,
    execute=_run_color_hunt,
    invitation="来玩个找颜色的游戏？我会亮一种颜色，你去环境里找同色的物品，找到了就算你赢。",
    rules=("狗接受后灯才重新选色并核对真实回执，邀请时的旧灯色不是接受前置条件。"
           "狗行动前须确认新灯色和安全路径；须导航确认停稳并视觉复核同一目标才算找到。"
           "判断输赢只看本局目标导航回执，之后自由探索到达别处不等于找到游戏目标。"),
    strategy=("结合自己的 traits、当前活动、同伴状态及新鲜环境观察决定是否邀请或接受。"
              "有清楚可辨认的同色静止物品可寻找时适合提议；有可玩项本身不是邀请理由。"
              "接受后灯依据真实环境观察自主选色，狗依据新观察自主选择候选，不预设颜色或物品。"),
    valid_invitation=valid_color_hunt_invitation,
    on_acceptance=_handle_color_hunt_acceptance,
    acceptance="我接受寻找同色物品。请先选颜色并设置灯，确认后我再找。",
)
