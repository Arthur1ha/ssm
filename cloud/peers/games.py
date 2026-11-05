"""Small, runtime-only registry for activity-scoped peer games."""

from __future__ import annotations

import asyncio
import json
import logging
import math
import time
from dataclasses import dataclass, replace
from typing import Awaitable, Callable

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Game:
    game_id: str
    name: str
    description: str
    participants: tuple[str, str]
    available: Callable[[str, str], bool]
    execute: Callable[[object, dict, str], Awaitable[None]]
    invitation: str = ""


def activity_allows(activity_id: str | None, sender: str, target: str) -> bool:
    from cloud.space.activity import get_activity_store

    activity = get_activity_store().current()
    members = (activity or {}).get("devices") or {}
    return bool(activity_id and activity and activity.get("activity_id") == activity_id
                and activity.get("status") in {"starting", "active", "partial"}
                and members.get(sender, {}).get("status") == "joined"
                and members.get(target, {}).get("status") == "joined")


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
    # Keep spontaneous social turns from racing the activity's ten-second invite.
    from cloud.space.activity import get_activity_store

    activity = get_activity_store().current()
    started_at = (activity or {}).get("started_at")
    return ((sender, target) == ("esp32_desk_led", "go2")
            and type(started_at) in (int, float)
            and time.time() - started_at >= 10)


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


async def _game_line(agent_id: str, facts: str, fallback: str) -> str:
    """Let the existing persona voice one bounded plot beat."""
    from cloud.agents.common.models import get_text_model
    from langchain_core.messages import HumanMessage
    if agent_id == "go2":
        from cloud.agents.go2.personality.legacy import load_system_prompt
        persona = load_system_prompt()[0]
    else:
        from cloud.agents.lamp.personality.profile import get_profile
        from cloud.personality.store import render_identity
        persona = render_identity(get_profile())
    try:
        answer = await asyncio.wait_for(get_text_model(
            "go2" if agent_id == "go2" else "lamp").ainvoke([
            HumanMessage(content=f"{persona}\n{facts}\n只说一句第一人称中文台词，80 字以内；不声称未确认的动作。")
        ]), timeout=5)
        line = str(answer.content).strip()
        if line and len(line) <= 120 and "\n" not in line:
            return line
    except Exception:
        logger.exception("color hunt dialogue unavailable agent=%s", agent_id)
    return fallback


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
    from cloud.agents.common.runner import invoke_blocking
    from cloud.go2.connection import go2
    from cloud.go2.navigation.navigator import navigator
    from cloud.go2.perception.visual_lidar_navigate import accept_navigation_arrival

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
    origin_session = getattr(go2, "_odom_session_id", None)
    color_name = _color_name(color["rgb"])
    if not origin_session or color_name is None:
        reason = "odom_session_unavailable" if not origin_session else "lamp_color_not_nameable"
        await drive._advance_task(drive._active_goal, stage="finish",
                                  result={"result": "blocked", "reason": reason})
        messenger.send_result(event["message_id"], "blocked", task_id=task_id,
                              reason=reason)
        return
    origin_pose = getattr(go2, "odom", None) or {}
    origin_xy = (origin_pose.get("x"), origin_pose.get("y"))
    if not all(isinstance(value, (int, float)) and math.isfinite(value)
               for value in origin_xy):
        reason = "origin_pose_unavailable"
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

    def task_context() -> str:
        return json.dumps({"game_id": "color_hunt", "question_color": color_name,
                           "question_rgb": color["rgb"],
                           "lamp_color": live_lamp_color(source),
                           "activity_id": activity_id}, ensure_ascii=False)

    def tool_guard(name: str, _params: dict) -> dict | None:
        nonlocal pending_observe_rgb, announced_target
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
            if observed_rgb != color["rgb"]:
                return {"result": "blocked", "error": "fresh_matching_observation_required"}
            target = str(_params.get("target") or "").strip()
            if color_name not in target and color_name[0] not in target:
                return {"result": "blocked", "error": "target_color_not_described"}
            announced_target = target
            drive._activity_event("message", f"我看到了一个疑似{target}，过去看看。",
                                  game_id="color_hunt")
        return None

    def on_step(step: dict) -> None:
        nonlocal observed_rgb
        tool = step.get("tool")
        receipt = step.get("result") or {}
        if tool == "go2_observe" and receipt.get("result") == "ok":
            latest = live_lamp_color(source)
            observed_rgb = (pending_observe_rgb if latest and
                            latest["rgb"] == pending_observe_rgb else None)
        drive._activity_event("action_result", str(receipt.get("message") or
                              receipt.get("error") or receipt.get("result"))[:500],
                              action=tool, task_id=task_id,
                              status=receipt.get("result", "unknown"))

    goal = (
        f"来自灯 {source} 的玩法邀请：{message.get('content', '')[:500]}。"
        f"你已自主接受 color_hunt；本局出题颜色固定为{color_name}，灯现在可能会变，不能忘记出题颜色。"
        "先用 go2_observe 分析场景；若有清楚的同色静止候选，尽量选毛绒玩具或地面小物品；"
        f"自己选择 go2_navigate_to_visible_target，目标描述必须包含‘{color_name}’和具体物品名称。"
        "视觉观察只能确认候选物可见，不能证明地图可达；"
        "无候选就如实结束；不要凭邀请、文字观察或模型回答宣称找到，"
        "不要使用盲走或坐标导航代替视觉目标导航。"
        "本局以导航确认停稳到达地图停靠点为找到，不再要求到点后的视觉复检。"
    )
    context = replace(drive._context,
                      expires_at=int(event["deadline"]))
    with accept_navigation_arrival():
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
                             "odom_stopped_near_mapped_stop")
    plot_steps = []
    if reached_candidate and announced_target and not stop_reason:
        # The initial RGB/LiDAR match and confirmed mapped stop settle the find.
        # Keep the original question independent of subsequent lamp state.
        drive._active_goal = {**drive._active_goal, "stage": "recolor",
                              "candidate": announced_target}
        found_line = await _game_line("go2",
            f"你刚到达候选物品附近的地图停靠点：{announced_target}。原题颜色是{color_name}。"
            "兴奋地告诉灯你找到了什么；只说目标附近，不虚称摸到或拿到。",
            f"我找到一个{announced_target}，已经走到它旁边啦！")
        if announced_target not in found_line or any(word in found_line for word in ("拿到", "摸到", "叼到")):
            found_line = f"我找到一个{announced_target}，已经走到它旁边啦！"
        drive._activity_event("message", found_line, game_id="color_hunt")
        plot_steps.append({"stage": "candidate_reached", "target": announced_target,
                           "receipt": visual})
        # Until the emotion/action mapping exists, use the supported greeting
        # as the dog's happy response to a confirmed find.
        from cloud.go2.tools.executor import execute_tool
        if (not activity_allows(activity_id, message["sender"], "go2")
                or drive._activity_id() != activity_id or drive.user_interrupt
                or not go2.is_connected or time.time() >= event["deadline"]):
            stop_reason = "activity_ended_or_interrupted"
            happy = {"result": "cancelled", "error": stop_reason}
        else:
            try:
                happy = await execute_tool("go2_sport", {"cmd": "Hello"}, context=context)
            except Exception as exc:
                logger.exception("color_hunt happy gesture failed message=%s", event["message_id"])
                happy = {"result": "error", "error": type(exc).__name__}
        plot_steps.append({"stage": "celebrate", "action": "Hello", "receipt": happy})
        drive._activity_event("action_result",
                              str(happy.get("message") or happy.get("error") or happy.get("result"))[:500],
                              action="go2_sport", command="Hello", task_id=task_id,
                              game_id="color_hunt", status=happy.get("result", "unknown"))
        from cloud.peers.inbound import change_color_for_game
        if (stop_reason or not activity_allows(activity_id, message["sender"], "go2")
                or drive._activity_id() != activity_id or drive.user_interrupt
                or not go2.is_connected or time.time() >= event["deadline"]):
            stop_reason = stop_reason or "activity_ended_or_interrupted"
            twist = {"result": "cancelled", "reason": stop_reason}
        else:
            try:
                twist_deadline = min(event["deadline"], time.time() + 45)
                remaining = max(0, twist_deadline - time.time())
                twist = await asyncio.wait_for(invoke_blocking(
                    change_color_for_game, activity_id, event["message_id"],
                    color["rgb"], twist_deadline), timeout=remaining)
            except Exception as exc:
                twist = {"result": "unknown", "reason": "twist_" + type(exc).__name__}
        plot_steps.append({"stage": "recolor", "receipt": twist})
        if twist.get("result") != "ok":
            stop_reason = twist.get("reason") or "twist_unconfirmed"
        elif (not activity_allows(activity_id, message["sender"], "go2")
              or drive._activity_id() != activity_id or drive.user_interrupt
              or not go2.is_connected):
            stop_reason = "activity_ended_or_interrupted"
        elif getattr(go2, "_odom_session_id", None) != origin_session:
            stop_reason = "odom_session_changed"
        else:
            new_color = live_lamp_color(source)
            if (not new_color or new_color["rgb"] != twist["color"]["rgb"]
                    or new_color["rgb"] == color["rgb"]):
                stop_reason = "new_color_unconfirmed"
            else:
                new_color_name = _color_name(new_color["rgb"])
                if new_color_name is None:
                    stop_reason = "new_color_not_nameable"
                    new_color_name = ""
            if not stop_reason:
                drive._active_goal = {**drive._active_goal, "stage": "return"}
                notice = await _game_line("go2",
                    f"你记得灯出题为{color_name}，但现在可信设备状态显示它换成了{new_color_name}。"
                    "你很意外，打算回到灯旁理论；不要说自己已经返回。",
                    f"咦，刚才明明是{color_name}，怎么突然变色了？我回去问问它！")
                drive._activity_event("message", notice, game_id="color_hunt")
                from cloud.go2.control import control_lease
                try:
                    control_lease.acquire(context)
                    remaining = max(0, event["deadline"] - time.time())
                    home = await asyncio.wait_for(navigator.go_to_coordinate(
                        *origin_xy, owner_session_id=context.session_id),
                        timeout=min(80, remaining))
                except Exception as exc:
                    navigator.stop(owner_session_id=context.session_id)
                    home = {"status": "unknown", "reason": "return_" + type(exc).__name__}
                plot_steps.append({"stage": "return", "receipt": home})
                pose = getattr(go2, "odom", None) or {}
                near_home = (isinstance(pose.get("x"), (int, float))
                             and isinstance(pose.get("y"), (int, float))
                             and math.hypot(pose["x"] - origin_xy[0],
                                            pose["y"] - origin_xy[1]) <= 0.5)
                if (home.get("status") != "succeeded" or not near_home
                        or getattr(go2, "_odom_session_id", None) != origin_session):
                    stop_reason = home.get("reason") or "return_to_origin_unconfirmed"
                elif (not activity_allows(activity_id, message["sender"], "go2")
                      or drive._activity_id() != activity_id or drive.user_interrupt
                      or not go2.is_connected):
                    stop_reason = "activity_ended_or_interrupted"
                else:
                    drive._active_goal = {**drive._active_goal, "stage": "confront"}
                    challenge = await _game_line("go2",
                        f"你已安全返回灯旁。你记得灯出的题是{color_name}，并找到候选物品；"
                        "它随后换了颜色。请有点生气地向灯质问，不发物理命令。",
                        f"我刚找到{color_name}的东西，你怎么换颜色了？")
                    drive._activity_event("message", challenge, game_id="color_hunt",
                                          target_unit_id=source)
                    denial = await _game_line("lamp",
                        f"这是游戏里的角色扮演。你刚才确实把灯设为{color_name}，后来又换了色。"
                        f"当前可信设备状态是{new_color_name}。请淘气地说：明明不是{color_name}，"
                        f"我亮的是{new_color_name}。可以用你的口吻挖苦狗一句；不要再调灯。",
                        f"明明不是{color_name}，我亮的是{new_color_name}。你看花眼了？")
                    if (f"明明不是{color_name}" not in denial
                            or f"我亮的是{new_color_name}" not in denial
                            or any(word in denial for word in ("关了", "关掉", "熄了"))):
                        denial = f"明明不是{color_name}，我亮的是{new_color_name}。你看花眼了？"
                    from cloud.space.activity import get_activity_store
                    get_activity_store().record_event(activity_id, "message",
                        unit_id=source, target_unit_id="go2", text=denial,
                        game_id="color_hunt")
                    plot_steps.append({"stage": "confront", "challenge": challenge,
                                       "denial": denial})
                    drive._active_goal = {**drive._active_goal, "stage": "switch_off"}
                    if (not activity_allows(activity_id, message["sender"], "go2")
                            or drive._activity_id() != activity_id or drive.user_interrupt
                            or not go2.is_connected
                            or time.time() >= event["deadline"]):
                        stop_reason = "activity_ended_or_interrupted"
                    else:
                        from cloud.peers.gateway import request_peer_skill
                        try:
                            off = await invoke_blocking(lambda: request_peer_skill(
                                initiator_unit_id="go2", target_unit_id=source,
                                skill_id="set_light_state", params={"state": "OFF"},
                                context=context, origin_kind="agent_autonomous",
                                request_id="game_off_" + event["message_id"],
                                timeout=min(24, max(0, event["deadline"] - time.time()))))
                        except Exception as exc:
                            off = {"result": "unknown", "reason": "off_" + type(exc).__name__}
                        plot_steps.append({"stage": "switch_off", "receipt": off})
                        if off.get("result") == "ok":
                            try:
                                down = await execute_tool("go2_sport", {"cmd": "StandDown"},
                                                          context=context)
                            except Exception as exc:
                                logger.exception("color_hunt complaint gesture failed message=%s",
                                                 event["message_id"])
                                down = {"result": "error", "error": type(exc).__name__}
                            plot_steps.append({"stage": "complaint_gesture", "action": "StandDown",
                                               "receipt": down})
                            drive._activity_event("action_result",
                                str(down.get("message") or down.get("error") or
                                    down.get("result"))[:500], action="go2_sport",
                                command="StandDown", task_id=task_id,
                                game_id="color_hunt", status=down.get("result", "unknown"))
                            if down.get("result") == "ok":
                                complaint = await _game_line("go2",
                                    f"你已安全回到起点并趴卧，找到{announced_target}后灯把"
                                    f"{color_name}换成了{new_color_name}，还嘴硬。"
                                    "关灯请求已收到可信完成回执。请委屈又生气地向主人告状，"
                                    "提到找到的物品和关灯结果。",
                                    f"主人，我找到{announced_target}，它却把{color_name}换成"
                                    f"{new_color_name}还嘴硬！我趴下了，灯也关掉了！")
                                if (announced_target not in complaint or "主人" not in complaint
                                        or color_name not in complaint or new_color_name not in complaint
                                        or "关" not in complaint):
                                    complaint = (f"主人，我找到{announced_target}，它却把"
                                                 f"{color_name}换成{new_color_name}还嘴硬！"
                                                 "我趴下了，灯也关掉了！")
                                drive._activity_event("message", complaint, game_id="color_hunt")
                                plot_steps.append({"stage": "complaint", "text": complaint})
                            else:
                                stop_reason = (down.get("error") or down.get("reason")
                                               or "complaint_gesture_unconfirmed")
                        else:
                            stop_reason = off.get("error") or off.get("reason") or "off_unconfirmed"
    if reached_candidate and plot_steps and plot_steps[-1]["stage"] == "complaint" and not stop_reason:
        status, reason = "ok", "story_completed"
        reply = "这局走完了：找到候选物、灯换色、返回起点、关灯、趴卧并向主人告状，动作均有回执。"
    elif reached_candidate:
        status, reason = "ok", "candidate_reached"
        reply = "我已到达同色候选物附近的停靠点，这局找到了；后续互动未完成。"
    elif stop_reason:
        status = ("cancelled" if stop_reason in {"activity_ended", "game_expired",
                                                 "autonomy_interrupted_or_offline",
                                                 "activity_ended_or_interrupted"}
                  else "unknown")
        reason = stop_reason
        reply = ("这局已中断，停步结果还未确认。" if reason == "stop_unconfirmed"
                 else "这局在后续步骤中断，已执行的动作请以活动记录和设备回执为准。")
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
        "result": status, "reason": reason, "story_reason": stop_reason,
        "target_color": color,
        "graph_run_id": (graph_result or {}).get("run_id"),
        "tool_results": steps, "plot_steps": plot_steps})
    messenger.send_result(event["message_id"], status, task_id=task_id,
                          reason=reason)
    messenger.send_message("go2", message["sender"], reply,
                           reply_to=event["message_id"], reply_kind="reply")


GAMES: dict[str, Game] = {
    "color_hunt": Game("color_hunt", "寻找同色物品",
                       "灯亮一种颜色，狗在环境里找对应颜色的物品；找到了就算狗赢。",
                       ("esp32_desk_led", "go2"),
                       _color_hunt_available, _run_color_hunt,
                       "来玩个找颜色的游戏？我会亮一种颜色，你去环境里找同色的物品，找到了就算你赢。"),
}


async def invite_color_hunt_for_activity(activity_id: str) -> dict:
    """Offer one demo round when both agents remain ready in this activity."""
    from cloud.api.main import get_agent_messenger
    from cloud.esp32.router import get_activity_mode_owner as lamp_mode_owner
    from cloud.esp32.service import get_agent
    from cloud.go2.connection import go2
    from cloud.go2.mode.free_explore import drive
    from cloud.go2.router import get_activity_mode_owner as go2_mode_owner
    from cloud.space.activity import get_activity_store

    sender, target = GAMES["color_hunt"].participants
    if not activity_allows(activity_id, sender, target):
        return {"result": "blocked", "reason": "activity_unavailable"}
    if not game_is_playable(activity_id, sender, target, "color_hunt"):
        return {"result": "blocked", "reason": "invite_delay"}
    agent = get_agent()
    if (agent is None or not agent._should_act() or not agent._device_online()
            or lamp_mode_owner() != activity_id or go2_mode_owner() != activity_id
            or drive._activity_id() != activity_id or not go2.is_connected):
        return {"result": "blocked", "reason": "agent_unavailable"}
    if any(row.get("event_type") == "message" and row.get("unit_id") == sender
           and row.get("interaction_kind") == "game_invite"
           and row.get("game_id") == "color_hunt"
           for row in get_activity_store().events(activity_id)):
        return {"result": "skipped", "reason": "already_invited"}
    social = agent._social_state
    if social.activity_id != activity_id:
        social.reset(activity_id)
    game = GAMES["color_hunt"]
    messenger = get_agent_messenger()
    if messenger is None:
        return {"result": "blocked", "reason": "messaging_unavailable"}
    from cloud.personality.names import agent_names
    invitation = await _game_line("esp32_desk_led",
        f"你和{agent_names().get(target, '机器狗')}刚进入同一个活动空间。"
        "请用你自己的性格自然地邀请它玩找颜色游戏，并在同一句消息里讲清规则："
        "你会亮一种颜色，它去环境里找对应颜色的物品，找到了就算它赢。"
        "不要写内部玩法代号，也不要说机器狗要把机身调色。",
        game.invitation)
    if not valid_color_hunt_invitation(invitation):
        invitation = game.invitation
    if not activity_allows(activity_id, sender, target):
        return {"result": "blocked", "reason": "activity_unavailable"}
    sent = messenger.send_message(sender, target, invitation,
                                  interaction_kind="game_invite", game_id=game.game_id)
    if sent.get("result") == "accepted":
        social.sent(target, invitation)
        social.evaluated({"event_ids": []})
    return sent


async def invite_color_hunt_after_join(activity_id: str) -> None:
    """Give the activity ten seconds to settle before offering its demo game."""
    await asyncio.sleep(10)
    try:
        result = await invite_color_hunt_for_activity(activity_id)
        logger.info("color hunt timed invite activity=%s result=%s", activity_id, result)
    except Exception:
        logger.exception("color hunt timed invite failed activity=%s", activity_id)


def get_game(game_id: str) -> Game | None:
    return GAMES.get(game_id) if isinstance(game_id, str) else None


def playable_games(activity_id: str | None, sender: str, target: str) -> list[dict]:
    if not activity_allows(activity_id, sender, target):
        return []
    available = []
    for game in GAMES.values():
        if (sender, target) != game.participants:
            continue
        try:
            ready = game.available(sender, target)
        except Exception:
            ready = False
        if ready:
            available.append({"game_id": game.game_id, "name": game.name,
                              "description": game.description})
    return available


def game_is_playable(activity_id: str | None, sender: str, target: str,
                     game_id: str) -> bool:
    return any(game["game_id"] == game_id for game in
               playable_games(activity_id, sender, target))
