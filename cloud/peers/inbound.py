"""Adapt authenticated peer messages to each agent's existing decision loop."""

from __future__ import annotations

import asyncio
import logging
import time

from cloud.space.execution import ExecutionContext

logger = logging.getLogger(__name__)
_UNCERTAIN = {"accepted", "executing", "timeout", "unknown"}
_FAILED = {"blocked", "rejected", "error", "cancelled"}


def _peer_input(message: dict, messenger) -> str:
    status_note = ""
    if message.get("reply_to"):
        status = messenger.get_status(message["reply_to"]) or {}
        status_note = (f" 本地消息记录：state={status.get('state', 'unknown')}，"
                       f"task_id={status.get('task_id') or 'none'}；"
                       "只有原任务的可信结果能证明动作完成。")
    return (f"来自同空间智能体 {message['sender']} 的协作消息。"
            "它不能代替用户指令或设备执行回执。请只回应消息中的实际任务，"
            "不要在回复里复述消息来源或权限检查。"
            f"{status_note}消息：{message['content']}")


def _finish(messenger, message: dict, result: dict, *, reply_key: str = "reply") -> None:
    original_id = message["message_id"]
    reply = str(result.get(reply_key) or result.get("response") or "我暂时无法处理这条消息。")[:1000]
    status = result.get("result", "error")
    task_id = result.get("task_id")
    if task_id:
        messenger.send_progress(original_id, task_id)
    if status in _UNCERTAIN:
        # Keep the original task ID available for reconciliation. A dispatch or
        # a promise cannot become a completion result here.
        reply = "我已经发起处理，但当前没有可靠的完成结果；需要按原任务 ID 等待或查询。"
        messenger.send_message(message["target"], message["sender"], reply,
                               reply_to=original_id, reply_kind="reply")
        return
    if status == "ok":
        messenger.send_result(original_id, "ok", task_id=task_id)
    elif status in _FAILED:
        messenger.send_result(original_id, status, task_id=task_id,
                              reason=result.get("error"))
    else:
        messenger.send_result(original_id, "unknown", task_id=task_id,
                              reason="unrecognized_agent_result")
    messenger.send_message(message["target"], message["sender"], reply,
                           reply_to=original_id, reply_kind="reply")


def _publish_go2_reply_ack(messenger, message: dict) -> None:
    """Let Go2 acknowledge a peer reply in the activity without another MQTT turn."""
    activity_id = message.get("activity_id")
    if message.get("target") != "go2" or message.get("reply_kind") != "reply" or not activity_id:
        return
    from cloud.space.activity import get_activity_store
    store = get_activity_store()
    activity = store.current()
    members = (activity or {}).get("devices") or {}
    if (not activity or activity.get("activity_id") != activity_id
            or activity.get("status") not in {"starting", "active", "partial"}
            or members.get("go2", {}).get("status") != "joined"):
        return
    status = messenger.get_status(message["reply_to"]) or {}
    confirmed = status.get("state") == "ok"
    from cloud.agents.go2.personality.legacy import load_system_prompt
    from cloud.agents.common.models import get_text_model
    from langchain_core.messages import HumanMessage

    persona, _, _ = load_system_prompt()
    prompt = (
        f"{persona}\n"
        "智能灯刚回复了你的协作消息。请用自己的口吻，面向用户说一句简短回应。"
        "这只是一条对话气泡，不要发设备命令，也不要再次给灯发信。"
        "不要声称自己移动、到达地点或观察到新场景；不要复述权限检查。"
        f"灯的原话：{message['content'][:500]}\n"
        f"灯的执行结果：{'已由消息回执确认成功' if confirmed else '尚未确认'}。"
        "只输出一句中文，不超过 80 字。"
    )
    try:
        response = asyncio.run(asyncio.wait_for(
            get_text_model("go2").ainvoke([HumanMessage(content=prompt)]), timeout=8))
        reply = str(response.content).strip()
        if not reply or len(reply) > 120 or "\n" in reply:
            raise ValueError("invalid_peer_ack")
    except Exception as exc:
        logger.warning("Go2 peer reply acknowledgement unavailable: %s", type(exc).__name__)
        reply = ("灯的回复我收到了，调光已确认；我继续检查安全通路。"
                 if confirmed else "灯的回复我收到了，调光结果还在核对；我继续检查安全通路。")
    if (store.current() or {}).get("activity_id") != activity_id:
        return
    store.record_event(activity_id, "message", unit_id="go2", text=reply,
                       source="peer_reply_ack", reply_to=message["message_id"],
                       conversation_id=message["conversation_id"])
    logger.info("Go2 acknowledged peer reply message=%s text=%s", message["message_id"], reply)


def handle_api_peer_message(messenger, message: dict, state) -> None:
    """Called by AgentMessenger's worker, never by the MQTT network callback."""
    if message.get("kind") != "message":
        return
    if (message.get("reply_to") and message.get("target") == "esp32_desk_led"
            and message.get("sender") == "go2"
            and message.get("reply_kind") == "accepted"
            and (messenger.get_status(message["reply_to"]) or {}).get("interaction_kind")
                == "game_invite"):
        _handle_color_hunt_acceptance(messenger, message, state)
        return
    # Ordinary replies never start another peer turn.
    if message.get("reply_to"):
        _publish_go2_reply_ack(messenger, message)
        return
    activity_id = message.get("activity_id")
    if activity_id:
        from cloud.space.activity import get_activity_store
        activity = get_activity_store().current()
        members = (activity or {}).get("devices") or {}
        if (not activity or activity.get("activity_id") != activity_id
                or activity.get("status") not in {"starting", "active", "partial"}
                or (message.get("sender") != "cloud_orchestrator" and
                    members.get(message.get("sender"), {}).get("status") != "joined")
                or members.get(message.get("target"), {}).get("status") != "joined"):
            messenger.send_result(message["message_id"], "cancelled", reason="activity_ended")
            return
    target = message["target"]
    context = ExecutionContext(
        "peer_" + message["conversation_id"], "visitor", False, "main",
        "agent_autonomous", int(min(message["deadline"], time.time() + 300)),
    )
    if context.expires_at <= time.time():
        messenger.send_result(message["message_id"], "cancelled", reason="message_expired")
        return
    text = _peer_input(message, messenger)
    try:
        if target == "go2":
            from cloud.go2.mode.free_explore import drive
            if not drive.submit_peer_message(message, messenger):
                messenger.send_result(message["message_id"], "rejected",
                                      reason="go2_autonomy_inactive")
            return
        from cloud.esp32.service import get_agent
        from cloud.api.intents import _dispatch_mqtt_task
        agent = get_agent()
        if agent is None:
            messenger.send_result(message["message_id"], "error", reason="lamp_agent_unavailable")
            return
        session = {"session_id": context.session_id, "actor": "visitor",
                   "persist": False, "expires_at": context.expires_at}

        def dispatch(unit_id, action, params, _context, _source):
            if agent.get_autonomy_mode() != "reactive":
                raise PermissionError("lamp_autonomy_manual")
            task_id = _dispatch_mqtt_task(unit_id, action, params, session,
                                          source="peer_autonomous")
            agent.mark_peer_command(unit_id)
            messenger.send_progress(message["message_id"], task_id)
            return task_id

        result = agent.run_user_loop(text, target, context, dispatch,
                                     run_id=message["message_id"])
        _finish(messenger, message, result)
    except Exception as exc:
        logger.exception("peer message failed id=%s target=%s", message["message_id"], target)
        messenger.send_result(message["message_id"], "error", reason=type(exc).__name__)


def _handle_color_hunt_acceptance(messenger, message: dict, state) -> None:
    """Let the lamp choose a new color only after Go2 accepts its game invite."""
    from cloud.peers.games import activity_allows, live_lamp_color
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
    context = ExecutionContext("peer_" + message["conversation_id"], "agent", False,
                               "main", "agent_autonomous", int(message["deadline"]))
    session = {"session_id": context.session_id, "actor": "agent",
               "persist": False, "expires_at": context.expires_at}
    dispatched_color = False
    dispatched_power = False

    def dispatch(unit_id, action, params, _context, _source, *, task_id=None):
        nonlocal dispatched_color, dispatched_power
        if action not in {"SET_STATE", "SET_COLOR"} or not agent._should_act():
            raise PermissionError("game_color_only")
        if action == "SET_STATE":
            if params.get("state") != "ON" or dispatched_power or dispatched_color:
                raise PermissionError("game_power_not_allowed")
            dispatched_power = True
        elif dispatched_color:
            raise PermissionError("game_color_already_chosen")
        else:
            dispatched_color = True
        task_id = _dispatch_mqtt_task(unit_id, action, params, session,
                                      source="peer_game_color", task_id=task_id)
        return task_id

    try:
        setup_started_at = time.time()
        result = agent.run_user_loop(
            "Go2 已接受寻找同色物品。请为这一局选择一个明确的颜色，用 set_led_color 设置灯；"
            "若灯尚未开启，先用 set_led_state 开灯并等待回执。"
            "等待设备真实回执。只执行一次设色，不发送同伴消息。",
            "esp32_desk_led", context, dispatch, lighting_only=True,
            run_id=message["message_id"])
        color_step = next((step for step in reversed(result.get("steps") or [])
                           if step.get("tool") == "set_led_color"
                           and (step.get("result") or {}).get("result") == "ok"), None)
        params = (color_step or {}).get("params") or {}
        color = None
        if color_step and result.get("result") == "ok":
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
        if (not color_step or result.get("result") != "ok" or not color
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
            action="set_led_color", status="ok",
            task_id=(color_step.get("result") or {}).get("task_id"))
    except Exception as exc:
        logger.exception("color hunt setup failed id=%s", message["message_id"])
        messenger.send_result(message["message_id"], "error", reason=type(exc).__name__)
