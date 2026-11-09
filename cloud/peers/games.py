"""Small, runtime-only registry for activity-scoped peer games."""

from __future__ import annotations

import asyncio
import json
import logging
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


GAMES: dict[str, Game] = {
    "color_hunt": Game("color_hunt", "寻找同色物品",
                       "灯亮一种颜色，狗在环境里找对应颜色的物品；找到了就算狗赢。",
                       ("esp32_desk_led", "go2"),
                       _color_hunt_available, _run_color_hunt,
                       "来玩个找颜色的游戏？我会亮一种颜色，你去环境里找同色的物品，找到了就算你赢。"),
}


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
