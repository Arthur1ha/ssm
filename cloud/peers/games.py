"""Small, runtime-only registry for activity-scoped peer games."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Awaitable, Callable


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
            return {"rgb": rgb, "as_of": entry["as_of"], "source": source}
    return None


def _color_hunt_available(sender: str, target: str) -> bool:
    if (sender, target) != ("esp32_desk_led", "go2"):
        return False
    from cloud.go2.connection import go2
    from cloud.go2.mode.free_explore import drive
    from cloud.go2.navigation.navigator import navigator
    from cloud.esp32.service import get_agent

    lamp = get_agent()
    if lamp is None or lamp.get_autonomy_mode() != "reactive":
        return False

    if not go2.is_connected or drive.user_interrupt or drive._pending_question_id:
        return False
    if (not drive._activity_id() or drive.owner_session_id != drive._activity_id()
            or drive._task is None or drive._task.done()):
        return False
    try:
        snapshot, _ = navigator._snapshot()
    except Exception:
        return False
    return bool(snapshot and live_lamp_color("esp32_desk_led"))


async def _run_color_hunt(drive: object, event: dict, task_id: str) -> None:
    """Bounded search; descriptions remain candidates without object evidence."""
    from cloud.go2.connection import go2

    message, messenger = event["message"], event["messenger"]
    color = live_lamp_color(str(event.get("source") or ""))
    if color is None:
        await drive._advance_task(drive._active_goal, stage="finish",
            result={"result": "rejected", "reason": "lamp_color_unknown_or_stale"})
        messenger.send_result(event["message_id"], "rejected", task_id=task_id,
                              reason="lamp_color_unknown_or_stale")
        messenger.send_message("go2", message["sender"],
            "我没读到这盏灯的新鲜 RGB 和亮度，先不按邀请文字猜颜色。",
            reply_to=event["message_id"], reply_kind="reply")
        return
    drive._active_goal = {**drive._active_goal, "kind": "game", "game_id": "color_hunt",
                          "target_color": color, "deadline": event["deadline"]}
    task_state = await drive._advance_task(drive._active_goal, stage="start")
    observations = []
    reason = "structured_multiframe_evidence_unavailable"
    color_round = 1
    while task_state.get("next_action") == "game_step":
        if not activity_allows(event.get("activity_id"), message["sender"], "go2"):
            reason = "activity_ended"
            break
        if drive.user_interrupt or not go2.is_connected:
            reason = "autonomy_interrupted_or_offline"
            break
        if time.time() >= event["deadline"]:
            reason = "game_expired"
            break
        latest = live_lamp_color(color["source"])
        if latest is None:
            reason = "lamp_color_stale"
            break
        if latest["rgb"] != color["rgb"]:
            color = latest
            color_round += 1
            drive._active_goal = {**drive._active_goal, "target_color": color,
                                  "color_round": color_round}
            task_state = await drive._advance_task(drive._active_goal,
                result={"result": "ok", "reason": "lamp_color_changed",
                        "target_color": color})
            continue
        drive._last_observation = None
        drive._last_tool_result = None
        await drive._do_explore(observe_question=(
            f"只描述当前画面中疑似与 RGB {color['rgb']} 同色的物品；"
            "不能把颜色描述当作已定位或已确认。"))
        if drive._last_observation:
            observations.append({"rgb": color["rgb"],
                                 "description": drive._last_observation[:240]})
        step_result = drive._last_tool_result or {"result": "unknown"}
        task_state = await drive._advance_task(drive._active_goal, result=step_result,
                                               step_done=True)
        if step_result.get("result") != "ok":
            reason = "frontier_unavailable_or_failed"
            break
    status = "cancelled" if reason in {"activity_ended", "autonomy_interrupted_or_offline"} else "unknown"
    await drive._advance_task(drive._active_goal, stage="finish", result={"result": status,
        "target_color": color, "color_rounds": color_round,
        "candidate_descriptions": observations, "reason": reason})
    messenger.send_result(event["message_id"], status, task_id=task_id,
                          reason=reason)
    if reason == "activity_ended":
        reply = "活动已结束，这局取消。"
    elif reason == "autonomy_interrupted_or_offline":
        reply = "这局已中断，不能确认后续动作结果。"
    else:
        detail = ("灯色状态已过期，需要重新确认。" if reason == "lamp_color_stale" else
                  "目前只有画面文字候选，缺少跨帧物体证据，不能确认找到。")
        reply = f"我跟随灯的实时颜色检查了 {color_round} 轮安全探索点。" + detail
    messenger.send_message("go2", message["sender"],
        reply,
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
