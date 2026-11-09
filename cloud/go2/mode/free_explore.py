"""
机器人内驱动系统：空闲时按好奇心定期探索地图边界。
视觉与 LLM 处理互动和到达后的场景描述，不控制地图导航的启动或停止。
"""
import asyncio
import logging
import re
import time
import uuid
from collections import deque
from dataclasses import replace
from enum import Enum
from typing import Optional

from cloud.go2.state import runtime_for
from cloud.go2.connection import go2
from cloud.go2.tools.tools import go2_observe
from cloud.go2.models import get_text_llm
from cloud.go2.tools.executor import execute_tool
from cloud.space.execution import ExecutionContext, SYSTEM_CONTEXT
from cloud.go2.perception.vision import VisionFrame
from cloud.agents.go2.personality.profile import get_profile
from cloud.agents.go2.memory.episode import episode_memory, EventType
from cloud.go2.space_context import prompt_space_context
from cloud.peers.social import SocialState, activity_peers
from cloud.agents.common.autonomous_visibility import AutonomousReplyGate, actionable_error

logger = logging.getLogger(__name__)

class MotivationalState(str, Enum):
    IDLE      = "IDLE"
    ALERT     = "ALERT"
    SOCIAL    = "SOCIAL"
    EXPLORING = "EXPLORING"


class Drive:
    """内驱动控制器，管理动机状态机和自主行为触发。"""

    def __init__(self) -> None:
        self._state           = MotivationalState.IDLE
        self._curiosity       = 0
        self._initial_exploration = False
        self._person_present   = False   # HOG 检测到人体（含远处背景人）
        self._person_engaging  = False   # 人脸检测到（近距且面向镜头，表示主动互动意图）
        self._last_person_ts   = 0.0
        self._last_action_ts   = 0.0
        self._social_tick      = 0
        self.user_interrupt    = False
        self._task: Optional[asyncio.Task] = None
        self._identity = None
        self.owner_session_id: str | None = None
        self._context: ExecutionContext = SYSTEM_CONTEXT
        self._last_execution_result: str = "ok"
        self._last_prompt_metadata: dict | None = None
        self._pending_question_id: str | None = None
        self._vision_retry_after = 0.0
        self._social_state = SocialState()
        self._social_retry_after = 0.0
        self._social_action_cooldowns: dict[str, float] = {}
        self._last_social_decision: dict | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._generation = 0
        self._events: deque[dict] = deque(maxlen=32)
        self._seen_events: set[str] = set()
        self._wake: asyncio.Event | None = None
        self._active_goal: dict | None = None
        self._last_decision: dict | None = None
        self._last_tool_result: dict | None = None
        self._last_observation: str | None = None
        self._action_cooldowns: dict[str, float] = {}
        self._reply_gate = AutonomousReplyGate()
        self._observation_gate = AutonomousReplyGate()

    @property
    def last_decision(self) -> dict | None:
        return self._last_decision

    def submit_event(self, event: dict) -> bool:
        """Thread-safe bounded fact delivery to the active autonomous loop."""
        if self._loop is None or self._task is None or self._task.done():
            return False
        event_id = str(event.get("event_id") or event.get("message_id") or "")
        ts = event.get("ts")
        deadline = event.get("deadline", (ts or 0) + 5)
        if not event_id or not isinstance(ts, (int, float)) or not isinstance(deadline, (int, float)):
            return False
        if ts > time.time() + 5 or deadline <= time.time():
            return False
        generation = self._generation
        def deliver() -> None:
            if generation != self._generation or event_id in self._seen_events:
                return
            if event.get("activity_id") and event["activity_id"] != self._activity_id():
                if event.get("kind") == "peer_message":
                    event["messenger"].send_result(event_id, "cancelled", reason="activity_ended")
                return
            self._seen_events.add(event_id)
            if len(self._seen_events) > 256:
                self._seen_events = {item.get("event_id") for item in self._events}
            if len(self._events) == self._events.maxlen:
                dropped = self._events.popleft()
                if dropped.get("kind") == "peer_message":
                    dropped["messenger"].send_result(dropped["message_id"], "cancelled",
                                                      reason="autonomy_queue_full")
            self._events.append(dict(event))
            if self._wake is not None:
                self._wake.set()
        self._loop.call_soon_threadsafe(deliver)
        return True

    def submit_peer_message(self, message: dict, messenger) -> bool:
        if message.get("target") != "go2" or message.get("reply_to"):
            return False
        return self.submit_event({"event_id": message["message_id"],
            "message_id": message["message_id"], "kind": "peer_message",
            "source": message.get("sender"), "content": message.get("content", ""),
            "interaction_kind": message.get("interaction_kind", "message"),
            "game_id": message.get("game_id"),
            "ts": time.time(), "deadline": message["deadline"],
            "activity_id": message.get("activity_id"),
            "message": message, "messenger": messenger})

    @staticmethod
    def _result_text(result: dict) -> str:
        return str(result.get("message") or result.get("error") or result.get("result"))

    def _activity_id(self) -> str | None:
        from cloud.space.activity import get_activity_store
        activity = get_activity_store().current()
        activity_id = activity.get("activity_id") if activity else None
        return activity_id if activity_id and self.owner_session_id == activity_id else None

    def _activity_event(self, event_type: str, text: str = "", **facts) -> dict | None:
        activity_id = self._activity_id()
        if activity_id:
            from cloud.space.activity import get_activity_store
            if event_type == "observation":
                facts.setdefault("source", "vision_description")
            goal = self._active_goal or {}
            if goal.get("kind") == "peer_message" and goal.get("event_id"):
                facts.setdefault("trigger_message_id", goal["event_id"])
            return get_activity_store().record_event(activity_id, event_type, unit_id="go2",
                                                     text=str(text)[:500], **facts)
        return None

    def _record_scene_observation(self, text: str, coordinate: tuple[float, float] | None,
                                  **facts) -> dict | None:
        """Share the first scene and later scenes reached in a different area."""
        activity_id = self._activity_id()
        if not activity_id or not text.strip() or text.startswith("无可用视频帧"):
            return None
        area = (("area", round(coordinate[0] / 2), round(coordinate[1] / 2))
                if coordinate is not None else ("unlocalized_view",))
        if not self._observation_gate.should_reply(
                activity_id, completed=True, material_key=area):
            return None
        event = self._activity_event("observation", self._scene_summary(text), **facts)
        if event:
            self._observation_gate.mark_replied(activity_id, completed=True, material_key=area)
        return event

    @staticmethod
    def _scene_summary(text: str) -> str:
        """Keep the vision model's factual notes compact in the observation card."""
        details = []
        for line in text.splitlines():
            line = re.sub(r"^[\s*#\-•]*(?:\d+[.)、]\s*)?", "", line).replace("**", "").strip()
            if not line or line.rstrip("：:") in {"场景描述（低视角）", "场景描述", "值得记录", "值得记录（导航/避障相关）"}:
                continue
            details.append(line)
            if len(details) == 3:
                break
        if len(details) > 1:
            return ("；".join(item.rstrip("。；;，,") for item in details) + "。")[:240]
        return (details[0] if details else text.strip())[:240]

    async def _publish_scene_reply(self, summary: str, observation_event_id: str) -> None:
        """Let Go2 speak about a recorded view, without turning raw vision into dialogue."""
        from cloud.agents.go2.workflows.reply import run_autonomous_reply_graph
        from cloud.api.intents import publish_agent_event

        generation, owner = self._generation, self.owner_session_id
        activity_id = self._activity_id()
        if not activity_id or not go2.is_connected or self.user_interrupt:
            return
        try:
            answer = await run_autonomous_reply_graph(
                run_id="scene_" + observation_event_id, action="go2_observe",
                reason="到达后观察周围", params={},
                result={"result": "ok", "message": summary},
                model=get_text_llm(), profile_loader=get_profile,
                observation=summary)
        except Exception:
            logger.exception("[Drive] 场景人格回复失败")
            return
        if (generation != self._generation or owner != self.owner_session_id
                or self._activity_id() != activity_id or self.user_interrupt
                or not go2.is_connected):
            return
        publish_agent_event("agent_reply", agent_id="go2", scope="space",
            space_id=self._context.space_id, source="autonomous", status="ok",
            run_id="scene_" + observation_event_id, text=answer,
            observation_event_id=observation_event_id)

    def _motion_announcement(self, name: str, params: dict, result: dict) -> tuple:
        scope = self._activity_id() or self.owner_session_id or "go2-autonomous"
        completed = result.get("result") == "ok"
        error = actionable_error(result)
        if not completed:
            material_key = None
        elif name in {"go2_navigate_to_coordinate", "go2_navigate_to"}:
            if "x" in params and "y" in params:
                # Nearby frontier targets are one area, not a new story each time.
                material_key = ("area", round(float(params["x"]) / 2),
                                round(float(params["y"]) / 2))
            else:
                material_key = ("destination", str(params.get("name") or result.get("target") or ""))
        elif name == "go2_sport":
            material_key = ("gesture", str(params.get("cmd") or ""))
        else:
            material_key = ("action", name)
        return scope, completed, material_key, error

    async def _evaluate_social(self, local_observation: dict | None = None) -> None:
        """A separate, non-motion decision at a Drive safety checkpoint."""
        if (self.user_interrupt or self._pending_question_id or not go2.is_connected
                or time.time() < self._social_retry_after):
            return
        activity_id = self._activity_id()
        if not activity_id:
            self._social_state.reset(None)
            return
        from cloud.space.context import get_space_context
        from cloud.api.main import _esp32_state
        context = get_space_context("go2", state=_esp32_state)
        activity, peers = activity_peers("go2", context["peers"])
        profile = get_profile()
        score = profile["traits"]["extraversion"]
        opportunity = self._social_state.opportunity(
            activity, peers, context["recent_discoveries"], score, unit_id="go2")
        if local_observation and local_observation.get("event_id") and peers:
            if local_observation["event_id"] not in self._social_state.seen_discoveries:
                opportunity = {"kind": "own_observation",
                               "event_ids": [local_observation["event_id"]],
                               "discoveries": [{"summary": local_observation.get("text", "")[:240],
                                                "event_id": local_observation["event_id"]}]}
        if not opportunity:
            return
        if not activity or not peers:
            return
        from cloud.agents.common.checkpoint import FileCheckpointSaver
        from cloud.agents.go2.workflows.social import build_social_graph
        from cloud.peers.games import playable_games
        from cloud.go2.tools.tools import send_message

        def can_send(activity_id_to_check: str, target: str, topic: str) -> bool:
            latest_activity, latest_peers = activity_peers(
                "go2", get_space_context("go2", state=_esp32_state)["peers"])
            return bool(latest_activity and latest_activity.get("activity_id") == activity_id_to_check
                        and target in {peer["unit_id"] for peer in latest_peers}
                        and self._social_state.can_send(target, topic)
                        and self._activity_id() == activity_id_to_check
                        and not self.user_interrupt and not self._pending_question_id
                        and go2.is_connected)

        async def deliver(target: str, content: str, *, interaction_kind: str = "message",
                          game_id: str | None = None) -> dict:
            outcome = (send_message(target, content, interaction_kind=interaction_kind,
                                    game_id=game_id) if interaction_kind == "game_invite"
                       else send_message(target, content))
            return outcome if isinstance(outcome, dict) else {"result": "error"}

        run_id = "social_" + uuid.uuid4().hex
        saver = FileCheckpointSaver()
        graph = build_social_graph(
            get_text_llm(), profile_loader=get_profile, can_send=can_send,
            send_message=deliver, checkpointer=saver)
        thread_id = "go2:social:" + run_id
        state = {"run_id": run_id, "activity_id": activity_id,
                 "activity": context["activity"], "opportunity": opportunity,
                 "peers": peers, "space_summary": prompt_space_context(),
                 "playable_games": [{"target": peer["unit_id"], **game}
                                    for peer in peers for game in playable_games(
                                        activity_id, "go2", peer["unit_id"])],
                 "active": bool(activity), "connected": go2.is_connected,
                 "interrupted": self.user_interrupt,
                 "pending_question": bool(self._pending_question_id),
                 "seen_event_ids": list(self._social_state.seen_discoveries)}
        try:
            async with saver.async_run_lock(thread_id):
                result = await graph.ainvoke(
                    state, {"configurable": {"thread_id": thread_id}}, durability="sync")
        except Exception as exc:
            logger.warning("[Drive/Social] evaluation failed: %s", exc)
            self._social_retry_after = time.time() + 30
            return
        if result.get("decision") and result.get("status") != "error":
            self._social_state.evaluated(opportunity)
        decision = result.get("decision") or {}
        if result.get("status") == "accepted":
            self._social_state.sent(str(decision.get("target") or ""),
                                    str(decision.get("reason") or decision.get("content") or ""))
            logger.info("[Drive/Social] sent target=%s", decision.get("target"))
        elif result.get("status") == "error":
            self._social_retry_after = time.time() + 30

    async def _execute_motion(self, name: str, params: dict, *, publish_reply: bool = True,
                              task_id: str | None = None) -> dict:
        if self.user_interrupt or not go2.is_connected or (runtime_for(go2) and go2.runtime.active_reason()):
            result = {"result": "cancelled", "message": "自主模式已中断或设备已断开"}
            if task_id:
                self._activity_event("action_result", result["message"], action=name,
                                     task_id=task_id, status="cancelled")
            return result
        self._activity_event("action_progress", "", action=name, status="executing",
                             task_id=task_id, display=bool(task_id))
        try:
            result = await execute_tool(name, params, context=self._context)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception("[Drive] action result unavailable tool=%s", name)
            result = {"result": "unknown", "error": type(exc).__name__,
                      "message": "动作结果尚未确认"}
        self._last_execution_result = result.get("result", "error")
        scope, completed, material_key, error = self._motion_announcement(name, params, result)
        visible = self._reply_gate.should_reply(
            scope, completed=completed, material_key=material_key, error=error)
        result_text = ("自由探索已到达新的地图探索点" if task_id and name == "go2_navigate_to_coordinate"
                       and completed else self._result_text(result))
        self._activity_event("action_result", result_text, action=name,
                             task_id=task_id or result.get("task_id"),
                             status=self._last_execution_result,
                             reason=error, display=bool(task_id) or visible)
        if publish_reply and go2.is_connected:
            asyncio.create_task(self._publish_motion_reply(name, params, result))
        return result

    async def _publish_motion_reply(self, name: str, params: dict, result: dict) -> None:
        from cloud.agents.go2.workflows.reply import run_autonomous_reply_graph
        from cloud.api.intents import publish_agent_event

        if not go2.is_connected or self.user_interrupt:
            return
        generation = self._generation
        owner = self.owner_session_id
        status = str(result.get("result") or "unknown")
        scope, completed, material_key, error = self._motion_announcement(name, params, result)
        if not self._reply_gate.should_reply(scope, completed=completed,
                                              material_key=material_key, error=error):
            return
        run_id = "auto_" + uuid.uuid4().hex
        publish_agent_event("agent_progress", agent_id="go2", scope="space",
            space_id=self._context.space_id, source="autonomous", status=status,
            run_id=run_id, step_id=run_id, stage="observed")
        answer = await run_autonomous_reply_graph(
            run_id=run_id, action=name, reason="根据环境自主探索与互动",
            params=params, result=result, model=get_text_llm(),
            profile_loader=get_profile)
        if (self.user_interrupt or not go2.is_connected or generation != self._generation
                or owner != self.owner_session_id
                or not self._reply_gate.mark_replied(scope, completed=completed,
                                                     material_key=material_key, error=error)):
            return
        publish_agent_event("agent_reply", agent_id="go2", scope="space",
            space_id=self._context.space_id, source="autonomous",
            status=status, run_id=run_id, text=answer)

    async def _navigate(self, name: str | None = None, coordinate: tuple[float, float] | None = None,
                        *, task_id: str | None = None, publish_reply: bool = True) -> dict:
        from cloud.go2.navigation.navigator import navigator
        owner_session_id = self.owner_session_id
        if (owner_session_id and owner_session_id.startswith("act_")
                and self._activity_id() != owner_session_id):
            if task_id:
                self._activity_event("action_result", "活动已结束，导航取消", action="go2_navigate_to_coordinate"
                                     if coordinate is not None else "go2_navigate_to",
                                     task_id=task_id, status="cancelled")
            return {"result": "cancelled", "reason": "activity_ended"}
        tool, params = (("go2_navigate_to_coordinate", {"x": coordinate[0], "y": coordinate[1]})
                        if coordinate is not None else ("go2_navigate_to", {"name": name}))
        nav_task = asyncio.create_task(self._execute_motion(
            tool, params, publish_reply=publish_reply, task_id=task_id))
        try:
            while not nav_task.done():
                if (self.user_interrupt or not go2.is_connected
                        or (owner_session_id and owner_session_id.startswith("act_")
                            and self._activity_id() != owner_session_id)):
                    nav_task.cancel()
                    await asyncio.gather(nav_task, return_exceptions=True)
                    if owner_session_id is not None:
                        navigator.stop(owner_session_id=owner_session_id)
                    stopped = False
                    if go2.is_connected:
                        try:
                            receipt = await go2.send_command("StopMove", completion_timeout=5.0)
                            stopped = receipt.get("result") == "ok"
                        except Exception:
                            pass
                    outcome = {"result": "cancelled" if stopped else "unknown",
                               "message": "导航被打断" if stopped else "停步未确认"}
                    if task_id:
                        self._activity_event("action_result", outcome["message"], action=tool,
                                             task_id=task_id, status=outcome["result"])
                    return outcome
                if self._events and await self._interrupt_for_event():
                    stopped = owner_session_id is not None and navigator.stop(owner_session_id=owner_session_id)
                    nav_task.cancel()
                    await asyncio.gather(nav_task, return_exceptions=True)
                    if stopped and go2.is_connected:
                        try:
                            receipt = await go2.send_command("StopMove", completion_timeout=5.0)
                            stopped = receipt.get("result") == "ok"
                        except Exception:
                            stopped = False
                    if not stopped:
                        self.user_interrupt = True
                    outcome = {"result": "cancelled" if stopped else "unknown",
                               "reason": "autonomous_task_handoff" if stopped else "stop_unconfirmed"}
                    if task_id:
                        self._activity_event("action_result", "导航已中断" if stopped else "停步尚未确认",
                                             action=tool, task_id=task_id, status=outcome["result"])
                    return outcome
                await asyncio.sleep(0.5)
            return nav_task.result()
        except asyncio.CancelledError:
            nav_task.cancel()
            await asyncio.gather(nav_task, return_exceptions=True)
            if owner_session_id is not None:
                navigator.stop(owner_session_id=owner_session_id)
            if task_id:
                self._activity_event("action_result", "导航已中断，停步结果尚未确认",
                                     action=tool, task_id=task_id, status="unknown")
            raise

    @property
    def state_snapshot(self) -> dict:
        return {
            "state":          self._state.value,
            "curiosity":      self._curiosity,
            "person_present": self._person_present,
            "last_action_ts": self._last_action_ts,
            "owner_session_id": self.owner_session_id,
        }

    # ── Vision 回调 ────────────────────────────────────────────────

    def on_vision_frame(self, frame: VisionFrame) -> None:
        """Record visual context for interaction without controlling map motion.

        _person_present：HOG 检测到人体（含远距坐着工作的人）
        _person_engaging：人脸检测到，表示对方近距且面向镜头，判定为主动互动意图
        人脸检测可能误判，不能让它独占自由探索或中断地图导航。
        """
        persons = frame.get('persons') or {}
        detected = persons.get('detected')
        if type(detected) is not bool or not isinstance(frame.get('faces'), dict):
            return
        currently_present  = detected
        currently_engaging = frame["faces"]["detected"]
        face_changed = currently_engaging != self._person_engaging

        # 追踪人体 presence，供对话上下文使用。
        if currently_present and not self._person_present:
            self._person_present = True
            episode_memory.add(
                EventType.VISION_CHANGE,
                f"检测到 {frame['persons']['count']} 人进入画面",
            )
        elif not currently_present and self._person_present:
            self._person_present = False
            self._last_person_ts = time.time()
            episode_memory.add(EventType.VISION_CHANGE, "画面中的人已离开")

        # 追踪主动互动信号（人脸可见）
        if currently_engaging and not self._person_engaging:
            self._person_engaging = True
            episode_memory.add(EventType.VISION_CHANGE, "检测到面向镜头的人，可评估互动")
        elif not currently_engaging and self._person_engaging:
            self._person_engaging = False

        if frame["changed"] and frame["change_type"] != "none" and not currently_present and not self._person_present:
            episode_memory.add(EventType.VISION_CHANGE, f"视觉变化：{frame['change_type']}")
        if (frame["changed"] or face_changed) and frame.get("frame_id"):
            self.submit_event({"event_id": "frame:" + str(frame["frame_id"]),
                "kind": "observation", "source": "go2.camera", "ts": frame["ts"],
                "deadline": frame["ts"] + 5,
                "content": f"persons={frame['persons']['count']}; faces={frame['faces']['count']}",
                "activity_id": self._activity_id()})

    def on_identity_event(self, event) -> None:
        """Keep identity as social context; person teachings have their own consumer."""
        self._identity = event.current

    def _identity_context(self) -> str:
        current = self._identity
        if current and current.status == "recognized":
            return f"当前人物：{current.name}（{current.role}，置信分 {current.score:.1f}）"
        if current and current.status == "unknown":
            return "当前人物：陌生人"
        return "当前人物：未确认"

    # ── 主循环 ─────────────────────────────────────────────────────

    async def _run_loop(self) -> None:
        """主驱动循环，每秒推进好奇心计数并根据状态触发对应行为。"""
        from cloud.agents.common.checkpoint import FileCheckpointSaver
        from cloud.agents.go2.workflows.free_explore import build_motivation_graph
        saver = FileCheckpointSaver()
        graph = build_motivation_graph(profile_loader=get_profile, checkpointer=saver)
        thread_id = "go2:motivation:" + uuid.uuid4().hex
        self._wake = asyncio.Event()
        self._wake.set()
        while True:
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=1)
            except asyncio.TimeoutError:
                pass
            self._wake.clear()
            if self.user_interrupt or self._pending_question_id:
                continue
            if self._events and not self._initial_exploration:
                event = self._events.popleft()
                await self._handle_event(event, graph, saver, thread_id)
                continue
            if time.time() < self._vision_retry_after:
                continue

            async with saver.async_run_lock(thread_id):
                tick = await graph.ainvoke(
                    {"state": self._state.value, "curiosity": self._curiosity,
                     "social_tick": self._social_tick,
                     "person_engaging": self._person_engaging,
                     "pending_event": {},
                     "interrupted": self.user_interrupt,
                     "pending_question": bool(self._pending_question_id),
                     "initial_exploration": self._initial_exploration,
                     "connected": go2.is_connected},
                    {"configurable": {"thread_id": thread_id}}, durability="sync")
            self._curiosity = tick.get("curiosity", self._curiosity)
            self._social_tick = tick.get("social_tick", self._social_tick)
            if tick.get("should_explore"):
                logger.info("[Drive] explore candidate revision=%s",
                            tick.get("profile_revision"))
                self._state = MotivationalState.EXPLORING
                try:
                    await self._do_explore()
                finally:
                    self._initial_exploration = False
                    self._state = MotivationalState.IDLE
            elif tick.get("should_social"):
                self.submit_event({"event_id": "social:" + uuid.uuid4().hex,
                    "kind": "social", "source": "go2.camera", "ts": time.time(),
                    "deadline": time.time() + 5})

    async def _decide_event(self, event: dict, graph=None, saver=None, thread_id=None) -> dict:
        from cloud.agents.common.checkpoint import FileCheckpointSaver
        from cloud.agents.go2.workflows.free_explore import build_motivation_graph
        saver = saver or FileCheckpointSaver()
        try:
            model = get_text_llm() if event.get("kind") == "peer_message" else None
        except Exception:
            model = None
        graph = build_motivation_graph(profile_loader=get_profile,
            checkpointer=saver, model=model)
        thread_id = thread_id or "go2:event:" + uuid.uuid4().hex
        public_event = {key: event.get(key) for key in
                        ("event_id", "kind", "source", "content", "deadline", "activity_id",
                         "interaction_kind", "game_id")}
        if event.get("interaction_kind") == "game_invite":
            from cloud.peers.games import get_game
            game = get_game(event.get("game_id"))
            public_event["game"] = ({"game_id": game.game_id, "name": game.name,
                                     "description": game.description} if game else None)
        async with saver.async_run_lock(thread_id):
            result = await graph.ainvoke({"pending_event": public_event,
                "space_summary": prompt_space_context(),
                "current_goal": self._active_goal,
                "last_tool_result": self._last_tool_result,
                "connected": go2.is_connected, "interrupted": self.user_interrupt,
                "pending_question": bool(self._pending_question_id)},
                {"configurable": {"thread_id": thread_id}}, durability="sync")
        self._last_decision = {"event_id": event["event_id"],
                               "choice": result.get("event_decision", "defer"),
                               "reason": result.get("decision_reason", "")}
        return self._last_decision

    async def _interrupt_for_event(self) -> bool:
        event = self._events[0]
        if event.get("kind") != "peer_message":
            return False
        decision = await self._decide_event(event)
        event["decision"] = decision
        return decision["choice"] == "accept"

    async def _advance_task(self, goal: dict, *, stage: str = "", result: dict | None = None,
                            step_done: bool = False) -> dict:
        """Persist one task transition; Drive only adapts the selected device step."""
        runtime = runtime_for(go2)
        if not hasattr(self, '_life_task_tokens'):
            self._life_task_tokens = {}
        event_id = goal['event_id']
        if runtime is not None:
            if stage == 'start':
                if runtime.active_reason():
                    return {'current_goal': {}, 'result': {'result': 'blocked', 'error': runtime.active_reason()}}
                self._life_task_tokens[event_id] = runtime.begin_life('play', self.owner_session_id, event_id)
            elif stage == 'finish':
                token = self._life_task_tokens.pop(event_id, None)
                if token:
                    runtime.finish_life(token)
        from cloud.agents.common.checkpoint import FileCheckpointSaver
        from cloud.agents.go2.workflows.free_explore import build_motivation_graph
        saver = FileCheckpointSaver()
        graph = build_motivation_graph(profile_loader=get_profile, checkpointer=saver)
        thread_id = "go2:task:" + goal["event_id"]
        update = {"event_id": goal["event_id"], "stage": stage,
                  "result": result or {}, "step_done": step_done, "now": time.time()}
        async with saver.async_run_lock(thread_id):
            state = await graph.ainvoke({"current_goal": goal, "task_update": update,
                "pending_event": {}, "connected": go2.is_connected,
                "interrupted": self.user_interrupt,
                "pending_question": bool(self._pending_question_id)},
                {"configurable": {"thread_id": thread_id}}, durability="sync")
        self._active_goal = state.get("current_goal") or None
        self._last_tool_result = state.get("last_tool_result")
        return state

    async def _handle_event(self, event: dict, graph, saver, thread_id) -> None:
        try:
            return await self._handle_event_owned(event, graph, saver, thread_id)
        finally:
            token = getattr(self, '_life_task_tokens', {}).pop(event.get('event_id'), None)
            runtime = runtime_for(go2)
            if runtime is not None and token:
                runtime.finish_life(token)

    async def _handle_event_owned(self, event: dict, graph, saver, thread_id) -> None:
        runtime = runtime_for(go2)
        if runtime is not None and runtime.active_reason():
            if event.get('kind') == 'peer_message':
                event['messenger'].send_result(event['message_id'], 'cancelled', reason='device_rest_or_offline')
            return
        if event.get("deadline", 0) <= time.time():
            if event.get("kind") == "peer_message":
                event["messenger"].send_result(event["message_id"], "cancelled", reason="message_expired")
            return
        decision = event.get("decision") or await self._decide_event(event, graph, saver, thread_id)
        if event["kind"] == "social":
            await self._do_social()
            return
        if event["kind"] != "peer_message":
            return
        messenger, message = event["messenger"], event["message"]
        from cloud.peers.games import activity_allows, game_is_playable, get_game
        activity_id = event.get("activity_id")
        is_game_invite = event.get("interaction_kind") == "game_invite"
        if ((activity_id or is_game_invite)
                and (not activity_allows(activity_id, message["sender"], "go2")
                     or (is_game_invite and self._activity_id() != activity_id))):
            messenger.send_result(event["message_id"], "cancelled", reason="activity_ended")
            return
        choice = decision["choice"]
        if choice != "accept":
            reason = str(decision.get("reason") or "decision_unavailable")[:200]
            logger.info("[Drive] peer proposal %s choice=%s reason=%s",
                        event["message_id"], choice, reason)
            if choice == "decline":
                messenger.send_result(event["message_id"], "rejected", reason=reason)
            else:
                messenger.send_result(event["message_id"], "unknown", reason=reason)
            return
        task_id = "peer_" + event["message_id"]
        game = None
        if event.get("interaction_kind") == "game_invite":
            game = get_game(event.get("game_id"))
            if not game or not game_is_playable(event.get("activity_id"),
                    message["sender"], "go2", game.game_id):
                messenger.send_result(event["message_id"], "rejected",
                                      reason="game_unavailable")
                return
            # MQTT QoS 1 can redeliver after a process restart. Reserve before
            # any game step so an unconfirmed prior run never repeats motion.
            from cloud.agents.common.effects import FileEffectLedger
            try:
                reservation = FileEffectLedger().reserve(
                    "go2", "go2:game:" + event["message_id"],
                    event["message_id"], "execute", game.game_id,
                    {"activity_id": activity_id, "sender": message["sender"]})
            except (OSError, ValueError) as exc:
                logger.warning("[Drive] game reservation unavailable: %s", type(exc).__name__)
                messenger.send_result(event["message_id"], "unknown",
                                      reason="game_reservation_unavailable")
                return
            if reservation["reservation"] == "existing":
                messenger.send_result(event["message_id"], "unknown",
                                      reason="prior_game_execution_unconfirmed")
                return
            acceptance_sent_at = time.time()
            acceptance = messenger.send_message(
                "go2", message["sender"], "我接受寻找同色物品。请先选颜色并设置灯，确认后我再找。",
                reply_to=event["message_id"], reply_kind="accepted")
            if acceptance.get("result") != "accepted":
                messenger.send_result(event["message_id"], "unknown",
                                      reason="acceptance_delivery_unconfirmed")
                return
            event["acceptance_message_id"] = acceptance["message_id"]
            event["acceptance_sent_at"] = acceptance_sent_at
        self._active_goal = {"kind": "peer_message", "event_id": event["event_id"],
                             "task_id": task_id, "deadline": event["deadline"]}
        messenger.send_progress(event["message_id"], task_id)
        if game is not None:
            try:
                await game.execute(self, event, task_id)
            except asyncio.CancelledError:
                from cloud.go2.navigation.navigator import navigator
                navigator.stop(owner_session_id=self.owner_session_id)
                stopped = False
                if go2.is_connected:
                    try:
                        receipt = await go2.send_command("StopMove", completion_timeout=5.0)
                        stopped = receipt.get("result") == "ok"
                    except Exception:
                        pass
                messenger.send_result(event["message_id"],
                                      "cancelled" if stopped else "unknown", task_id=task_id,
                                      reason="autonomy_stopped" if stopped else "stop_unconfirmed")
                raise
            except Exception as exc:
                logger.exception("[Drive] game failed game_id=%s", game.game_id)
                from cloud.go2.navigation.navigator import navigator
                navigator.stop(owner_session_id=self.owner_session_id)
                if go2.is_connected:
                    try:
                        await go2.send_command("StopMove", completion_timeout=5.0)
                    except Exception:
                        pass
                messenger.send_result(event["message_id"], "unknown", task_id=task_id,
                                      reason="game_error:" + type(exc).__name__)
            finally:
                self._active_goal = None
            return
        from cloud.agents.go2.graph import run_go2_loop
        from cloud.peers.inbound import _peer_input
        context = replace(self._context, expires_at=int(min(event["deadline"], time.time() + 90)))
        task = asyncio.create_task(run_go2_loop(context.session_id, _peer_input(message, messenger),
                context=context, run_id=event["message_id"], deadline=event["deadline"],
                publish_reply=False))
        try:
            while not task.done():
                if (self.user_interrupt or not go2.is_connected
                        or time.time() >= event["deadline"]
                        or (activity_id and not activity_allows(
                            activity_id, message["sender"], "go2"))):
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
                    from cloud.go2.navigation.navigator import navigator
                    navigator.stop(owner_session_id=self.owner_session_id)
                    if go2.is_connected:
                        try:
                            await go2.send_command("StopMove", completion_timeout=5.0)
                        except Exception:
                            pass
                    result = {"result": "cancelled" if self.user_interrupt else "unknown",
                              "error": "autonomy_interrupted" if self.user_interrupt else "task_expired_or_offline"}
                    break
                await asyncio.sleep(0.2)
            else:
                result = await task
        except asyncio.CancelledError:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            messenger.send_result(event["message_id"], "cancelled", task_id=task_id,
                                  reason="autonomy_stopped")
            raise
        except Exception as exc:
            logger.exception("[Drive] peer task failed")
            result = {"result": "unknown", "error": type(exc).__name__}
        await self._advance_task(self._active_goal, stage="finish", result=result)
        self._active_goal = None
        status = result.get("result", "unknown")
        if status not in {"ok", "blocked", "rejected", "error", "cancelled"}:
            status = "unknown"
        messenger.send_result(event["message_id"], status, task_id=task_id,
                              reason=result.get("error"))
        if result.get("response"):
            messenger.send_message(message["target"], message["sender"],
                str(result["response"])[:1000], reply_to=event["message_id"], reply_kind="reply")

    async def _do_explore(self, observe_question: str = "描述当前位置看到的场景，有什么值得记录的") -> None:
        runtime = runtime_for(go2)
        if runtime is not None and (runtime.active_reason() or not runtime.exploration_enabled):
            return
        token = runtime.begin_life('explore', self.owner_session_id, 'explore_' + __import__('uuid').uuid4().hex) if runtime else None
        try:
            return await self._do_explore_owned(observe_question)
        finally:
            if token:
                runtime.finish_life(token)

    async def _do_explore_owned(self, observe_question: str = "描述当前位置看到的场景") -> None:
        """Lifecycle entry; the shared graph owns exploration decisions."""
        if self.user_interrupt or self._pending_question_id or time.time() < self._vision_retry_after:
            return
        from cloud.agents.go2.workflows.search import run_search, OBSERVATION_TIMEOUT_S
        generation, owner = self._generation, self.owner_session_id
        last_observation = None

        def current():
            return (generation == self._generation and owner == self.owner_session_id
                    and go2.is_connected and not self.user_interrupt
                    and (not owner or not owner.startswith("act_") or self._activity_id() == owner))

        async def share(observation):
            nonlocal last_observation
            if not current():
                return
            summary = (observation.get("observation") or {}).get("scene_summary")
            if not isinstance(summary, str) or not summary.strip():
                return
            pose = observation.get("pose") or {}
            coordinate = (pose["x"], pose["y"]) if "x" in pose and "y" in pose else None
            event = self._record_scene_observation(summary, coordinate,
                frame_id=observation.get("frame_id"), media_ref=observation.get("media_ref"),
                source="go2.camera")
            if event:
                self._last_observation = summary
                last_observation = event

        result = await run_search(mode="explore", context=self._context, on_observation=share)
        if not current():
            return
        # Localization may be unavailable at entry. A fresh camera
        # observation remains useful, without granting any movement permission.
        if self._initial_exploration and result.get("reason") == "map_session_unavailable":
            from cloud.go2.perception.visual_target import observe_target
            from cloud.space.execution import use_context
            try:
                with use_context(self._context):
                    sample = await asyncio.wait_for(observe_target("", require_pose=False),
                                                    timeout=OBSERVATION_TIMEOUT_S)
                await share({**sample, "frame_id": sample["frame"]["frame_id"]})
            except Exception as exc:
                logger.warning("[Drive] entry observation unavailable: %s", exc)
            if not current():
                return
            self._activity_event("action_result", "自主探索尚未开始，定位尚未就绪。",
                action="explore", task_id=result.get("task_id"), status=result["result"],
                reason=result["reason"])
        self._last_tool_result = result
        self._last_action_ts = time.time()
        if result.get("reason") in {"coverage_exhausted", "no_reachable_observation_point", "map_session_unavailable"}:
            self._vision_retry_after = time.time() + 60
        if current() and last_observation:
            try:
                await self._evaluate_social(last_observation)
            except Exception:
                logger.exception("[Drive/Social] observation sharing decision failed")

    async def _do_social(self) -> None:
        """Social opportunity uses the same bounded event handling checkpoint."""
        await self._evaluate_social()

    # ── 生命周期 ───────────────────────────────────────────────────

    def pause_for_user(self, owner_session_id: str | None = None) -> bool:
        """Cancel current work, retaining permission for a fresh later decision."""
        if owner_session_id is not None and self.owner_session_id != owner_session_id:
            return False
        runtime = runtime_for(go2)
        allowed = runtime.exploration_enabled if runtime is not None else False
        paused = (self.owner_session_id, self._context,
                  runtime.data['epoch'] if runtime is not None else None)
        self._pause_in_progress = True
        try:
            stopped = self.stop(owner_session_id=owner_session_id)
            if stopped and allowed and paused[0] is not None:
                self._paused_exploration = paused
                runtime.exploration_enabled = True
            return stopped
        finally:
            self._pause_in_progress = False

    def resume_after_user(self) -> bool:
        if getattr(self, '_pause_in_progress', False) or getattr(self, '_user_handoff_in_progress', False):
            return False
        paused = getattr(self, '_paused_exploration', None)
        runtime = runtime_for(go2)
        if not paused or runtime is None:
            return False
        if (not runtime.exploration_enabled or runtime.data['epoch'] != paused[2]
                or runtime.data['autonomy_mode'] != 'free_explore'):
            self._paused_exploration = None
            return False
        if (runtime.active_reason() or runtime.data['life_state'] != 'idle'
                or runtime.data['control_mode'] != 'normal' or go2.pending_action
                or go2.body_state != 'standing' or go2.body_ready_reason()):
            return False
        from cloud.go2.control import control_lease
        from cloud.go2.navigation.navigator import navigator
        lease = control_lease.snapshot()
        if lease is not None and lease.owner_session_id != paused[0]:
            return False
        nav_task = getattr(navigator, '_task', None)
        if isinstance(nav_task, asyncio.Task) and not nav_task.done():
            return False
        if go2._command_lock.locked():
            return False
        self._paused_exploration = None
        self.start(paused[0], paused[1])
        return True

    def start(self, owner_session_id: str = "system", context: ExecutionContext | None = None) -> None:
        """启动内驱动循环，重置所有状态，创建后台任务。"""
        if self._task is not None:
            self._task.cancel()
        runtime = runtime_for(go2)
        if runtime is not None:
            if runtime.active_reason():
                return
            runtime.exploration_enabled = True
        self._state            = MotivationalState.IDLE
        self._curiosity        = 0
        self._initial_exploration = True
        self._vision_retry_after = 0.0
        self._person_present   = False
        self._person_engaging  = False
        self._task             = asyncio.create_task(self._run_loop())
        self._loop = asyncio.get_running_loop()
        self._generation += 1
        self._events.clear()
        self._seen_events.clear()
        self._reply_gate.reset()
        self._observation_gate.reset()
        self.owner_session_id  = owner_session_id
        self._context = replace(context or SYSTEM_CONTEXT, origin_kind="agent_autonomous")
        self.user_interrupt = False
        self._pending_question_id = None
        logger.info("[Drive] 内驱动循环已启动")

    def stop(self, owner_session_id: str | None = None) -> bool:
        """停止内驱动循环，取消后台任务，同时停止 navigator 避免孤儿导航 task。"""
        if owner_session_id is not None and self.owner_session_id != owner_session_id:
            return False
        self._paused_exploration = None
        runtime = runtime_for(go2)
        if runtime is not None:
            runtime.exploration_enabled = False
            if runtime.data['life_state'] == 'explore' and runtime.data['life_owner'] == self.owner_session_id:
                runtime.finish_life((runtime.data['epoch'], runtime.data['life_task_id']))
            for token in getattr(self, '_life_task_tokens', {}).values():
                runtime.finish_life(token)
            self._life_task_tokens = {}
        owned_session = self.owner_session_id
        active = self._task is not None
        self.user_interrupt = True
        self._generation += 1
        for event in self._events:
            if event.get("kind") == "peer_message":
                event["messenger"].send_result(event["message_id"], "cancelled",
                                               reason="autonomy_stopped")
        self._events.clear()
        self._loop = None
        self._pending_question_id = None
        if active:
            self._task.cancel()
            self._task = None
        self.owner_session_id = None
        from cloud.go2.navigation.navigator import navigator
        if active and owned_session is not None:
            navigator.stop(owner_session_id=owned_session)
        logger.info("[Drive] 内驱动循环已停止")
        return True


drive = Drive()
