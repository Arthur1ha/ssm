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
    # An invitation is a proposal. Color and motion prerequisites belong to
    # the execution stage, after Go2 has accepted the game.
    return (sender, target) == ("esp32_desk_led", "go2")


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
    drive._active_goal = {**drive._active_goal, "kind": "game", "game_id": "color_hunt",
                          "target_color": color, "deadline": event["deadline"]}
    await drive._advance_task(drive._active_goal, stage="start")
    observed_rgb = None
    pending_observe_rgb = None

    def task_context() -> str:
        return json.dumps({"game_id": "color_hunt", "lamp_color": live_lamp_color(source),
                           "activity_id": activity_id}, ensure_ascii=False)

    def tool_guard(name: str, _params: dict) -> dict | None:
        nonlocal pending_observe_rgb
        if not activity_allows(activity_id, message["sender"], "go2"):
            return {"result": "cancelled", "error": "activity_ended"}
        if drive.user_interrupt or not go2.is_connected:
            return {"result": "cancelled", "error": "autonomy_interrupted_or_offline"}
        latest = live_lamp_color(source)
        if latest is None:
            return {"result": "blocked", "error": "lamp_color_stale"}
        if name == "go2_observe":
            pending_observe_rgb = latest["rgb"]
        elif name == "go2_navigate_to_visible_target" and observed_rgb != latest["rgb"]:
            return {"result": "blocked", "error": "fresh_matching_observation_required"}
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
        "你已自主接受 color_hunt：观察当前画面，按本轮可信任务状态中的实时灯 RGB "
        "寻找可能同色的静止物品。先用 go2_observe 分析场景；若有清楚的候选，"
        "自己选择 go2_navigate_to_visible_target 并给出具体目标描述。"
        "无候选就如实结束；不要凭邀请、文字观察或模型回答宣称找到，"
        "不要使用盲走或坐标导航代替视觉目标导航。"
    )
    context = replace(drive._context,
                      expires_at=int(min(event["deadline"], time.time() + 180)))
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
    reached_candidate = bool(visual and visual.get("result") == "ok" and
                             visual.get("confirmation") ==
                             "odom_stopped_and_rgb_lidar_reobserved")
    if stop_reason:
        status = ("cancelled" if stop_reason in {"activity_ended", "game_expired",
                                                 "autonomy_interrupted_or_offline"}
                  else "unknown")
        reason = stop_reason
        reply = ("这局已中断，停步结果还未确认。" if reason == "stop_unconfirmed"
                 else "这局已中断，没有继续执行游戏动作。")
    elif reached_candidate:
        status, reason = "unknown", "color_match_unverified"
        reply = "我已按视觉与地图回执走到候选物品附近；它是否真的与灯同色还缺少可信颜色证据。"
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
        "result": status, "reason": reason, "target_color": live_lamp_color(source),
        "graph_run_id": (graph_result or {}).get("run_id"),
        "tool_results": steps})
    messenger.send_result(event["message_id"], status, task_id=task_id,
                          reason=reason)
    messenger.send_message("go2", message["sender"], reply,
                           reply_to=event["message_id"], reply_kind="reply")


GAMES: dict[str, Game] = {
    "color_hunt": Game("color_hunt", "寻找同色物品",
                       "灯改变颜色，狗按实时灯色寻找同色物品；缺少跨帧证据时不能确认找到。",
                       ("esp32_desk_led", "go2"),
                       _color_hunt_available, _run_color_hunt),
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
