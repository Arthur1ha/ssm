"""Find a matching object: rules, strategy, validation and execution."""
from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import replace

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .base import Game, activity_allows
from .role_graph import run_device_task
from cloud.agents.common.emotions import EmotionModel
from cloud.space.execution import ExecutionContext

logger = logging.getLogger(__name__)


class InvitationDecision(EmotionModel, BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    accepted: bool
    speech: str = Field(min_length=1, max_length=300)

    @field_validator("speech")
    @classmethod
    def nonblank(cls, value):
        if not value.strip():
            raise ValueError("invitation_response_required")
        return value.strip()

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
    return {sender, target} == {"esp32_desk_led", "go2"}


def _task_result(tool: str, params: dict, receipt: dict) -> dict:
    """Only confirmed arrival satisfies this device's color-hunt subtask."""
    from cloud.go2.tools.registry import target_navigation_complete

    complete = (tool == "go2_navigate_to_visible_target"
                and target_navigation_complete(receipt, str(params.get("target") or "")))
    return {**receipt, "final": complete or receipt.get("result") == "cancelled"}


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
    if event.get("game_preaccepted"):
        # Only the trusted acceptance adapter adds this after the lamp's
        # physical setup succeeds. A message body cannot set this flag.
        prepared = event.get("prepared_color")
        current = live_lamp_color("esp32_desk_led")
        if prepared and current and current["rgb"] == prepared["rgb"]:
            return current, ""
        return None, "lamp_color_unknown_or_stale"
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


async def _run_color_hunt(drive: object, event: dict, task_id: str) -> dict | None:
    """Let Go2's existing graph choose observations and visual navigation."""
    from cloud.go2.connection import go2
    from cloud.go2.navigation.navigator import navigator

    message, messenger = event["message"], event["messenger"]
    source = str(event.get("source") or "")
    activity_id = event.get("activity_id")
    generation = getattr(drive, "_generation", 0)
    owner = getattr(drive, "owner_session_id", None)
    context = replace(drive._context, expires_at=int(event["deadline"]))
    task_goal = dict(drive._active_goal or {
        "kind": "peer_message", "event_id": event.get("event_id") or event["message_id"],
        "task_id": task_id})
    task_goal.setdefault("generation", generation)
    task_goal.setdefault("owner_session_id", owner)
    task_goal.setdefault("activity_id", activity_id)

    def current(*, include_game=True):
        from .scope import game_scope_available
        return (generation == getattr(drive, "_generation", 0)
                and owner == getattr(drive, "owner_session_id", None)
                and drive._activity_id() == activity_id
                and (not include_game or activity_allows(activity_id, message["sender"], "go2"))
                and (not include_game or game_scope_available()))

    async def finish(status, reason, **facts):
        from .scope import begin_game_close
        begin_game_close()
        # Keep the original task even if a newer activity has replaced the
        # Drive's shared goal while we waited for the lamp/model/device.
        await drive._advance_task(task_goal, stage="finish",
                                  result={"result": status, "reason": reason, **facts})

    async def stop_original():
        """A stop request is not fresh evidence that the body has stopped."""
        if not current(include_game=False) or not go2.is_connected:
            return {"result": "unknown", "reason": "control_context_changed"}
        try:
            return await navigator.stop_and_confirm(owner_session_id=context.session_id)
        except Exception as exc:
            return {"result": "unknown", "reason": type(exc).__name__}

    if not activity_allows(activity_id, message["sender"], "go2"):
        messenger.send_result(event["message_id"], "cancelled", task_id=task_id,
                              reason="activity_ended")
        return
    color, color_error = await _wait_for_lamp_color(event, drive)
    if not current():
        await finish("cancelled", "activity_ended")
        messenger.send_result(event["message_id"], "cancelled", task_id=task_id,
                              reason="activity_ended")
        return
    if color is None:
        await finish("rejected", color_error)
        messenger.send_result(event["message_id"], "rejected", task_id=task_id,
                              reason=color_error)
        return
    try:
        snapshot, snapshot_error = navigator._snapshot()
    except Exception:
        snapshot, snapshot_error = None, "map_unavailable"
    if not go2.is_connected or snapshot is None:
        reason = "go2_disconnected" if not go2.is_connected else snapshot_error or "map_unavailable"
        await finish("blocked", reason)
        messenger.send_result(event["message_id"], "blocked", task_id=task_id,
                              reason=reason)
        return
    color_name = _color_name(color["rgb"])
    if color_name is None:
        reason = "lamp_color_not_nameable"
        await finish("blocked", reason)
        messenger.send_result(event["message_id"], "blocked", task_id=task_id,
                              reason=reason)
        return
    task_goal.update(kind="game", game_id="color_hunt", target_color=color,
                     deadline=event["deadline"], stage="search")
    drive._active_goal = task_goal
    started = await drive._advance_task(task_goal, stage="start")
    if not current():
        await finish("cancelled", "activity_ended")
        messenger.send_result(event["message_id"], "cancelled", task_id=task_id,
                              reason="activity_ended")
        return
    if (started.get("result") or {}).get("result") == "blocked":
        messenger.send_result(event["message_id"], "blocked", task_id=task_id,
                              reason=(started["result"].get("error") or "game_unavailable"))
        return
    from cloud.space.activity import get_activity_store
    invitation_id = event.get("original_invitation_id") or event["message_id"]
    get_activity_store().record_event(activity_id, "game_started", unit_id="go2",
        game_id="color_hunt", trigger_message_id=invitation_id, task_id=task_id,
        status="executing", source_event_id="game:started:" + invitation_id)
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
                           "activity_id": activity_id}, ensure_ascii=False)

    def tool_guard(name: str, _params: dict) -> dict | None:
        nonlocal pending_observe_rgb, announced_target, candidate_basis
        if not current():
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
        return None

    def on_step(step: dict) -> None:
        nonlocal observed_rgb, observed_evidence
        if not current():
            return
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
                    context_scope="process",
                    trigger_message_id=event["message_id"])
                if observation and observation.get("event_id"):
                    observed_rgb = pending_observe_rgb
                    observed_evidence = {"ref": observation["event_id"], "kind": "observation",
                        "source": "go2", "as_of": observation["ts"],
                        "summary": observation_text.strip()[:240]}
        drive._activity_event("action_result", str(receipt.get("message") or
                              receipt.get("error") or receipt.get("result"))[:500],
                              action=tool, task_id=task_id, game_id="color_hunt",
                              context_scope="process",
                              trigger_message_id=event["message_id"],
                              **({"decision_basis": candidate_basis}
                                 if tool == "go2_navigate_to_visible_target" and candidate_basis else {}),
                              status=receipt.get("result", "unknown"))

    goal = (
        f"来自灯 {source} 的玩法邀请：{message.get('content', '')[:500]}。"
        f"你已自主接受 color_hunt；本局已由真实回执确认出题颜色为{color_name}。"
        "先用 go2_observe 重新分析当前场景；依据真实观察自行选择清楚可辨认的同色静止候选；"
        f"自己选择 go2_navigate_to_visible_target，目标描述必须包含‘{color_name}’和具体物品名称。"
        "在导航的decision_reason中简短记录当前观察为何支持这个同色候选，依据留在过程详情，无需额外发言。"
        "视觉观察只能确认候选物可见，不能证明地图可达；"
        "无候选就如实结束；不要凭邀请、文字观察或模型回答宣称找到，"
        "不要使用盲走或坐标导航代替视觉目标导航。"
        "本局须工具确认已在同一目标附近并停稳才算完成，不能把仅看见或推进一段当成完成。"
    )
    graph_task = asyncio.create_task(run_device_task(
        "go2", goal, context=context,
        run_id=event["message_id"], deadline=event["deadline"],
        allowed_tools=frozenset({"go2_observe", "go2_navigate_to_visible_target",
                                 "get_space_context"}),
        tool_guard=tool_guard, task_context=task_context, on_step=on_step,
        result_transform=_task_result,
        availability=current, parent_run_id=invitation_id, share_result=False))
    graph_result = None
    stop_reason = None
    stop_receipt = None
    try:
        while not graph_task.done():
            if not current():
                stop_reason = "activity_ended"
            elif drive.user_interrupt or not go2.is_connected:
                stop_reason = "autonomy_interrupted_or_offline"
            elif time.time() >= event["deadline"]:
                stop_reason = "game_expired"
            if stop_reason:
                from .scope import begin_game_close
                begin_game_close()
                graph_task.cancel()
                await asyncio.gather(graph_task, return_exceptions=True)
                stop_receipt = await stop_original()
                if stop_receipt.get("result") != "ok":
                    stop_reason = "stop_unconfirmed"
                break
            await asyncio.sleep(0.2)
        if not stop_reason:
            graph_result = await graph_task
            if not current():
                stop_reason = "activity_ended"
            elif drive.user_interrupt or not go2.is_connected:
                stop_reason = "autonomy_interrupted_or_offline"
            elif time.time() >= event["deadline"]:
                stop_reason = "game_expired"
    except asyncio.CancelledError:
        from .scope import begin_game_close
        begin_game_close()
        graph_task.cancel()
        await asyncio.gather(graph_task, return_exceptions=True)
        raise
    except Exception as exc:
        from .scope import begin_game_close
        begin_game_close()
        logger.exception("color_hunt graph failed message=%s", event["message_id"])
        stop_receipt = await stop_original()
        stop_reason = "game_graph_error" if stop_receipt.get("result") == "ok" else "stop_unconfirmed"
        graph_result = {"result": "unknown", "error": type(exc).__name__}

    steps = (graph_result or {}).get("tool_results") or []
    visual = next((step.get("result") or {} for step in reversed(steps)
                   if step.get("tool") == "go2_navigate_to_visible_target"), None)
    from cloud.go2.tools.registry import target_navigation_complete
    reached_candidate = bool(announced_target and visual and
                             target_navigation_complete(visual, announced_target))
    if reached_candidate and not stop_reason:
        status, reason = "ok", "candidate_reached"
        reply = str((graph_result or {}).get("response") or "").strip()[:1000]
    elif stop_reason:
        status = ("cancelled" if stop_reason in {"activity_ended", "game_expired",
                                                 "autonomy_interrupted_or_offline",
                                                 "activity_ended_or_interrupted"}
                  else "unknown")
        reason = stop_reason
        reply = ""
    else:
        status = (graph_result or {}).get("result", "unknown")
        if status not in {"blocked", "rejected", "error", "cancelled"}:
            status = "unknown"
        last_receipt = (steps[-1].get("result") or {}) if steps else {}
        reason = (last_receipt.get("error") or last_receipt.get("reason") or
                  (graph_result or {}).get("error") or "no_confirmed_color_match")
        reply = str((graph_result or {}).get("response") or "").strip()[:1000]
    if status != "ok" and stop_receipt is None:
        from .scope import begin_game_close
        begin_game_close()
        stop_receipt = await stop_original()
        if stop_receipt.get("result") != "ok":
            status, reason = "unknown", "stop_unconfirmed:" + reason
    if reply:
        get_activity_store().record_event(activity_id, "task_result", unit_id="go2",
            text=reply, game_id="color_hunt", trigger_message_id=invitation_id,
            task_id=task_id, context_scope="shared",
            status=status, public_result={"result": status, "reason": reason},
            emotion=(graph_result or {}).get("emotion", "neutral"),
            source_event_id="game:final:" + invitation_id)
    await finish(status, reason, target_color=color,
                 graph_run_id=(graph_result or {}).get("run_id"), tool_results=steps,
                 **({"stop": stop_receipt} if stop_receipt is not None else {}))
    messenger.send_result(event["message_id"], status, task_id=task_id,
                          reason=reason)
    return {"result": status, "reason": reason,
            "role_replies": {"go2": {"speech": reply, "status": status, "terminal": status == "ok",
                "emotion": (graph_result or {}).get("emotion", "neutral")}} if reply else {}}


def _handle_color_hunt_acceptance(messenger, message: dict, state) -> None:
    """Let the lamp choose a new color only after Go2 accepts its game invite."""
    from cloud.space.context import get_space_context
    from cloud.space.decisions import decision_evidence, resolve_decision
    from cloud.space.activity import get_activity_store

    original = messenger.get_status(message["reply_to"]) or {}
    if (original.get("sender"), original.get("target")) == ("go2", "esp32_desk_led"):
        return _queue_reverse_color_hunt(messenger, message, original)
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
    return _prepare_lamp_color(messenger, message, state)


def _queue_reverse_color_hunt(messenger, message: dict, original: dict) -> None:
    from cloud.go2.mode.free_explore import drive
    from cloud.peers.games.base import game_invitation_ready
    from cloud.space.activity import get_activity_store
    from cloud.esp32.physical_tasks import get as get_physical_task

    activity_id = message.get("activity_id")
    if (original.get("interaction_kind") != "game_invite"
            or original.get("game_id") != "color_hunt"
            or original.get("state") not in {"waiting_start", "waiting_result"}
            or (message.get("sender"), message.get("target")) != ("esp32_desk_led", "go2")
            or original.get("activity_id") != activity_id
            or min(message.get("deadline", 0), original.get("deadline", 0)) <= time.time()
            or not game_invitation_ready(activity_id, "go2", "esp32_desk_led")):
        messenger.send_result(message["message_id"], "rejected", reason="invalid_game_acceptance")
        return
    # This event can only be emitted by the real preparation helper after it
    # has checked edge results, fresh state and the actual observation refs.
    prepared = next((item for item in reversed(get_activity_store().events(activity_id))
                     if item.get("game_id") == "color_hunt"
                     and item.get("trigger_message_id") == message["reply_to"]
                     and item.get("event_type") == "action_result"
                     and item.get("action") == "set_led_color" and item.get("status") == "ok"), None)
    color = live_lamp_color("esp32_desk_led")
    recorded = (prepared or {}).get("state_at_event") or {}
    values = recorded.get("values") or {}
    receipt = (get_physical_task(prepared["task_id"], unit_id="esp32_desk_led",
                                session_id="peer_game_color_" + original["conversation_id"])
               if prepared and prepared.get("task_id") else None)
    if (not prepared or not color or not receipt or receipt.get("result") != "ok"
            or recorded.get("status") != "current"
            or tuple(values.get(key) for key in ("r", "g", "b")) != color["rgb"]
            or values.get("brightness") != color["brightness"]
            or not 0 <= time.time() - prepared.get("ts", 0) <= 60):
        messenger.send_result(message["message_id"], "rejected", reason="lamp_color_not_confirmed")
        return
    if not drive.submit_accepted_game(message, original, messenger, color):
        messenger.send_result(message["message_id"], "rejected", reason="go2_autonomy_inactive")


def _handle_color_hunt_invitation(messenger, message: dict, state) -> None:
    """The lamp may accept, defer or decline a dog's signed invitation."""
    from cloud.agents.common.effects import FileEffectLedger
    from cloud.peers.games.base import game_invitation_ready
    from cloud.esp32.service import get_agent

    activity_id = message.get("activity_id")
    if (message.get("game_id") != "color_hunt"
            or message.get("interaction_kind") != "game_invite"
            or (message.get("sender"), message.get("target")) != ("go2", "esp32_desk_led")
            or message.get("deadline", 0) <= time.time()
            or not game_invitation_ready(activity_id, "go2", "esp32_desk_led")):
        messenger.send_result(message["message_id"], "rejected", reason="game_participant_busy_or_expired")
        return
    agent = get_agent()
    if agent is None:
        messenger.send_result(message["message_id"], "blocked", reason="lamp_agent_unavailable")
        return
    try:
        reservation = FileEffectLedger().reserve(
            "esp32_desk_led", "lamp:game:" + message["message_id"], message["message_id"],
            "accept", "color_hunt", {"activity_id": activity_id, "sender": "go2"})
        if reservation["reservation"] != "new":
            messenger.send_result(message["message_id"], "unknown", reason="prior_game_execution_unconfirmed")
            return
        decision = _lamp_invitation_decision(agent, message)
        if decision.get("event_decision") != "accept":
            messenger.send_result(message["message_id"], "rejected", reason="invitation_declined")
            speech = str(decision.get("decision_speech") or "").strip()
            if speech:
                messenger.send_message("esp32_desk_led", "go2", speech,
                                       reply_to=message["message_id"], reply_kind="declined",
                                       emotion=decision.get("decision_emotion", "neutral"))
            return
        if (time.time() >= message["deadline"]
                or not game_invitation_ready(activity_id, "go2", "esp32_desk_led")):
            messenger.send_result(message["message_id"], "cancelled", reason="game_participant_busy_or_expired")
            return

        class PreparationResult:
            result = None

            def send_progress(self, *_args, **_kwargs):
                pass

            def send_result(self, _message_id, result, *, task_id=None, reason=None):
                self.result = {"result": result, "task_id": task_id, "reason": reason}

        preparation = PreparationResult()
        _prepare_lamp_color(preparation, {**message, "reply_to": message["message_id"]}, state)
        result = preparation.result or {"result": "unknown", "reason": "lamp_color_not_confirmed"}
        if result["result"] != "ok":
            messenger.send_result(message["message_id"], result["result"], reason=result.get("reason"))
            return
        if (not activity_allows(activity_id, "go2", "esp32_desk_led")
                or time.time() >= message["deadline"]
                or not game_invitation_ready(activity_id, "go2", "esp32_desk_led")):
            messenger.send_result(message["message_id"], "cancelled", reason="activity_ended_or_expired")
            return
        response = str(decision.get("decision_speech") or "").strip()
        if not response:
            messenger.send_result(message["message_id"], "unknown", reason="acceptance_response_unavailable")
            return
        sent = messenger.send_message("esp32_desk_led", "go2", response,
                                      reply_to=message["message_id"], reply_kind="accepted",
                                      deadline=message["deadline"],
                                      emotion=decision.get("decision_emotion", "neutral"))
        if sent.get("result") != "accepted":
            messenger.send_result(message["message_id"], "unknown", reason="acceptance_delivery_unconfirmed")
    except Exception as exc:
        logger.exception("lamp game acceptance failed id=%s", message["message_id"])
        messenger.send_result(message["message_id"], "error", reason=type(exc).__name__)


def _lamp_invitation_decision(agent, message: dict) -> dict:
    """Ask the lamp's own Graph whether it wants to play this invitation."""
    from cloud.space.context import get_space_context
    from .role_graph import decide

    epochs = (getattr(agent, "_autonomy_epoch", None), getattr(agent, "_command_epoch", None))
    result = asyncio.run(decide("lamp", "invite", {
        "game": GAME.metadata(), "message": message.get("content", ""),
        "space": get_space_context("esp32_desk_led", state=agent._state)},
        schema=InvitationDecision, instructions=(
            "依据自身人格、当前事实和邀请，自主决定现在是否愿意玩寻找同色物品。"
            "接受仅代表参与本局准备；接受后再根据真实观察选择颜色。"
            "愿意返回accepted=true，否则false；speech只自然回答邀请，不复述规则或内部回执。"),
        thread_id="lamp:color:invite:" + message["message_id"],
        deadline=message["deadline"], availability=lambda: (
            time.time() < message["deadline"]
            and activity_allows(message.get("activity_id"), "go2", "esp32_desk_led")
            and epochs == (getattr(agent, "_autonomy_epoch", None),
                           getattr(agent, "_command_epoch", None)))))
    return {"event_decision": "accept" if result["accepted"] else "decline",
            "decision_speech": result["speech"], "decision_emotion": result.get("emotion", "neutral")}


def _prepare_lamp_color(messenger, message: dict, state) -> None:
    """Shared physical setup after either side has accepted the same game."""
    from cloud.space.context import get_space_context
    from cloud.space.decisions import decision_evidence, resolve_decision
    from cloud.space.activity import get_activity_store

    activity_id = message.get("activity_id")
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
        get_activity_store().record_event(activity_id, "action_result", unit_id="esp32_desk_led",
            target_unit_id="go2", status="blocked", reason="game_observation_required",
            fact_kind="result", result_kind="action", game_id="color_hunt",
            trigger_message_id=message["reply_to"])
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
            "依据只保留在过程详情，不作为同伴发言：按你的性格用第一人称简短记录选择依据，"
            "把视觉发现归于实际观察者，不声称自己看到了同伴的画面；不写内部字段或工具名。"
            "evidence_refs 必须引用 space.decision_evidence 中相关 observation 的 ref。"
            "没有相关观察或观察不能支持所选颜色时，如实说明并结束，不猜颜色或编造物品。"
            "若灯尚未开启，先用 set_led_state 开灯并等待回执，开灯调用声明final=false继续原目标；"
            "开灯也必须附上本次选色的理由和观察引用。"
            "等待设备真实回执。只执行一次设色，不发送同伴消息，不预设之后变色或其他剧情。")
        result = asyncio.run(run_device_task(
            "lamp", setup_goal, context=context, dispatch=dispatch,
            run_id=message["message_id"], deadline=message["deadline"],
            allowed_tools=frozenset({"set_led_state", "set_led_color"}),
            result_transform=lambda name, _params, receipt: {
                **receipt, "final": receipt.get("result") == "cancelled"
                or name == "set_led_color" and receipt.get("result") == "ok"},
            parent_run_id=message["reply_to"], share_result=False,
            availability=lambda: (agent._should_act() and agent._device_online()
                and activity_allows(activity_id, "esp32_desk_led", "go2")
                and time.time() < message["deadline"])))
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
            unit_id="esp32_desk_led", text="", fact_kind="result", result_kind="action",
            public_result=color_step["result"], arguments=color_step.get("params") or {},
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
    bidirectional=True,
    requires_idle=True,
    available=_color_hunt_available,
    execute=_run_color_hunt,
    rules=("灯或狗都可邀请，双方均无未完成任务且空闲时才可发起；接收方依据人格、当前事实自主接受、拒绝或延后。"
           "双方确认接受后灯才重新选色并核对真实回执，邀请时的旧灯色不是接受前置条件。"
           "狗行动前须确认新灯色和安全路径；须导航确认停稳并视觉复核同一目标，或自动降级找物确认同一目标已在附近且停稳，才算找到。"
           "判断输赢只看本局目标导航回执，之后自由探索到达别处不等于找到游戏目标。"),
    strategy=("结合自己的 traits、当前活动、同伴状态及新鲜环境观察决定是否邀请或接受。"
              "有清楚可辨认的同色静止物品可寻找时适合提议；有可玩项本身不是邀请理由。"
              "接受后灯依据真实环境观察自主选色，狗依据新观察自主选择候选，不预设颜色或物品。"),
    on_acceptance=_handle_color_hunt_acceptance,
    on_invitation=_handle_color_hunt_invitation,
    acceptance="",
)
