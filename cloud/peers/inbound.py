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
    if message.get("interaction_kind") == "game_invite":
        from cloud.peers.games import get_game
        game = get_game(message.get("game_id"))
        if game:
            reply = reply.replace(game.game_id, game.name)
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
    from cloud.agents.go2.personality.profile import get_profile
    from cloud.personality.store import render_identity
    from cloud.agents.common.models import get_text_model, async_model_clients
    from langchain_core.messages import HumanMessage

    persona = render_identity(get_profile())
    prompt = (
        f"{persona}\n"
        "智能灯刚回复了你的协作消息。请用自己的口吻，面向用户说一句简短回应。"
        "这只是一条对话气泡，不要发设备命令，也不要再次给灯发信。"
        "不要声称自己移动、到达地点或观察到新场景；不要复述权限检查。"
        f"灯的原话：{message['content'][:500]}\n"
        f"灯的执行结果：{'已由消息回执确认成功' if confirmed else '尚未确认'}。"
        "只输出一句中文，不超过 80 字。"
    )
    @async_model_clients()
    async def acknowledge():
        return await asyncio.wait_for(
            get_text_model("go2").ainvoke([HumanMessage(content=prompt)]), timeout=8)

    try:
        response = asyncio.run(acknowledge())
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
    if message.get("kind") == "result":
        _relay_reverse_game_result(messenger, message)
        return
    if message.get("kind") != "message":
        return
    if (message.get("reply_to")
            and message.get("reply_kind") == "accepted"
            and (messenger.get_status(message["reply_to"]) or {}).get("interaction_kind")
                == "game_invite"):
        from cloud.peers.games import get_game
        original = messenger.get_status(message.get("reply_to")) or {}
        game = get_game(original.get("game_id"))
        if (game and game.on_acceptance
                and game.allows_pair(original.get("sender"), original.get("target"))
                and (message.get("target"), message.get("sender")) ==
                    (original.get("sender"), original.get("target"))):
            game.on_acceptance(messenger, message, state)
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
    if message.get("interaction_kind") == "game_invite" and target != "go2":
        from cloud.peers.games import get_game
        from cloud.peers.games.base import game_invitation_ready
        game = get_game(message.get("game_id"))
        if (not game or not game.allows_pair(message.get("sender"), target)
                or not game.available(message.get("sender"), target)):
            messenger.send_result(message["message_id"], "rejected", reason="game_unavailable")
        elif not game_invitation_ready(activity_id, message.get("sender"), target):
            messenger.send_result(message["message_id"], "rejected", reason="game_participant_busy")
        elif game.on_invitation:
            game.on_invitation(messenger, message, state)
        else:
            messenger.send_result(message["message_id"], "rejected", reason="game_receiver_unavailable")
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

        def dispatch(unit_id, action, params, _context, _source, *, task_id=None):
            if agent.get_autonomy_mode() != "reactive":
                raise PermissionError("lamp_autonomy_manual")
            task_id = _dispatch_mqtt_task(unit_id, action, params, session,
                                          source="peer_autonomous", task_id=task_id)
            agent.mark_peer_command(unit_id)
            messenger.send_progress(message["message_id"], task_id)
            return task_id

        result = agent.run_user_loop(text, target, context, dispatch,
                                     run_id=message["message_id"])
        _finish(messenger, message, result)
    except Exception as exc:
        logger.exception("peer message failed id=%s target=%s", message["message_id"], target)
        messenger.send_result(message["message_id"], "error", reason=type(exc).__name__)


from cloud.peers.games.color_hunt import _handle_color_hunt_acceptance


def _relay_reverse_game_result(messenger, message: dict) -> None:
    """The lamp relays its courier's real result to the dog's original invite."""
    from cloud.peers.games import get_game
    accepted = messenger.get_status(message.get("message_id")) or {}
    original_id = accepted.get("reply_to")
    original = messenger.get_status(original_id) if original_id else None
    if not original:
        return
    game = get_game(original.get("game_id"))
    if (not game or not game.bidirectional
            or original.get("interaction_kind") != "game_invite"
            or accepted.get("reply_kind") != "accepted"
            or (original.get("sender"), original.get("target")) != tuple(reversed(game.participants))
            or (accepted.get("sender"), accepted.get("target")) != game.participants
            or (message.get("sender"), message.get("target")) != tuple(reversed(game.participants))
            or accepted.get("activity_id") != original.get("activity_id")
            or original.get("state") not in {"waiting_start", "waiting_result"}):
        return
    result = accepted.get("result") or {}
    if result.get("result") in {"ok", "blocked", "rejected", "cancelled", "unknown", "error"}:
        messenger.send_result(original_id, result["result"], task_id=accepted.get("task_id"),
                              reason=result.get("reason"))
