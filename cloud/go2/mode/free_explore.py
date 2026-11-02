"""
机器人内驱动系统：空闲时按好奇心定期探索地图边界。
视觉与 LLM 处理互动和到达后的场景描述，不控制地图导航的启动或停止。
"""
import asyncio
import logging
import time
import uuid
from collections import deque
from dataclasses import replace
from enum import Enum
from typing import Optional

from cloud.go2.connection import go2
from cloud.go2.tools.tools import go2_observe, get_text_llm
from cloud.go2.tools.executor import execute_tool
from cloud.go2.execution import ExecutionContext, SYSTEM_CONTEXT
from cloud.go2.perception.vision import VisionFrame
from cloud.agents.go2.personality.legacy import get_system_prompt
from cloud.agents.go2.personality.profile import get_profile
from cloud.agents.go2.memory.episode import episode_memory, EventType
from cloud.go2.space_context import prompt_space_context
from cloud.go2.navigation import frontier as frontier_mod
from cloud.peers.social import SocialState, activity_peers

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
        self._explore_visited: list[tuple[float, float]] = []
        self._explore_rejected: list[tuple[float, float]] = []
        self._last_observation: str | None = None
        self._action_cooldowns: dict[str, float] = {}

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
            return get_activity_store().record_event(activity_id, event_type, unit_id="go2",
                                                     text=str(text)[:500], **facts)
        return None

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

        async def deliver(target: str, content: str) -> dict:
            outcome = send_message(target, content)
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

    async def _execute_motion(self, name: str, params: dict, *, publish_reply: bool = True) -> dict:
        if self.user_interrupt or not go2.is_connected:
            return {"result": "cancelled", "message": "自主模式已中断或设备已断开"}
        self._activity_event("action_progress", name, action=name, status="executing")
        result = await execute_tool(name, params, context=self._context)
        self._last_execution_result = result.get("result", "error")
        self._activity_event("action_result", self._result_text(result), action=name,
                             task_id=result.get("task_id"), status=self._last_execution_result)
        if publish_reply and go2.is_connected:
            asyncio.create_task(self._publish_motion_reply(name, params, result))
        return result

    async def _publish_motion_reply(self, name: str, params: dict, result: dict) -> None:
        from cloud.agents.go2.workflows.reply import run_autonomous_reply_graph
        from cloud.api.intents import publish_agent_event

        if not go2.is_connected or self.user_interrupt:
            return
        run_id = "auto_" + uuid.uuid4().hex
        status = str(result.get("result") or "unknown")
        publish_agent_event("agent_progress", agent_id="go2", scope="space",
            space_id=self._context.space_id, source="autonomous", status=status,
            run_id=run_id, step_id=run_id, stage="observed")
        answer = await run_autonomous_reply_graph(
            run_id=run_id, action=name, reason="根据环境自主探索与互动",
            params=params, result=result, model=get_text_llm(),
            profile_loader=get_profile)
        if self.user_interrupt or not go2.is_connected:
            return
        publish_agent_event("agent_reply", agent_id="go2", scope="space",
            space_id=self._context.space_id, source="autonomous",
            status=status, run_id=run_id, text=answer)

    async def _navigate(self, name: str | None = None, coordinate: tuple[float, float] | None = None) -> dict:
        from cloud.go2.navigation.navigator import navigator
        owner_session_id = self.owner_session_id
        tool, params = (("go2_navigate_to_coordinate", {"x": coordinate[0], "y": coordinate[1]})
                        if coordinate is not None else ("go2_navigate_to", {"name": name}))
        nav_task = asyncio.create_task(self._execute_motion(tool, params))
        try:
            while not nav_task.done():
                if self.user_interrupt or not go2.is_connected:
                    nav_task.cancel()
                    await asyncio.gather(nav_task, return_exceptions=True)
                    if owner_session_id is not None:
                        navigator.stop(owner_session_id=owner_session_id)
                    return {"result": "cancelled", "message": "导航被打断"}
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
                    return {"result": "cancelled" if stopped else "unknown",
                            "reason": "autonomous_task_handoff" if stopped else "stop_unconfirmed"}
                await asyncio.sleep(0.5)
            return nav_task.result()
        except asyncio.CancelledError:
            nav_task.cancel()
            await asyncio.gather(nav_task, return_exceptions=True)
            if owner_session_id is not None:
                navigator.stop(owner_session_id=owner_session_id)
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
        currently_present  = frame["persons"]["detected"]
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
        """Only confirmed fresh identity can trigger an enabled teaching."""
        self._identity = event.current
        current, previous = event.current, event.previous
        if (current.status != "recognized" or current.role != "owner" or
                previous.status == "recognized" and previous.person_id == current.person_id):
            return
        from cloud.go2.perception.vision import vision_loop
        frame, _ = vision_loop.latest_frame_pair()
        if frame is None or abs(time.time() - frame["ts"]) > 5:
            return
        from cloud.agents.common import teachings
        from cloud.agents.common.teaching_bindings import candidates
        from cloud.cards.registry import get_registry
        card = get_registry().get_card("go2")
        if not card:
            return
        observation = {"id": "vision.owner_recognized", "values": {"recognized": True},
                       "source": "go2.identity", "ts": current.ts,
                       "person_id": current.person_id}
        matches = candidates(teachings.list_for("go2", space_id=self._context.space_id),
                             observation, card)
        for match in matches:
            binding = match["binding"]
            if binding["skill_id"] == "go2_sport" and binding["params"].get("cmd") == "Hello":
                self.submit_event({"event_id": f"owner:{current.person_id}:{current.ts}:{match['teaching_id']}",
                    "kind": "teaching", "source": "go2.identity", "ts": time.time(),
                    "deadline": time.time() + 5, "person_id": current.person_id,
                    "teaching_id": match["teaching_id"], "action": "Hello"})

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
        while True:
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=1)
            except asyncio.TimeoutError:
                pass
            self._wake.clear()
            if self.user_interrupt or self._pending_question_id:
                continue
            if self._events:
                event = self._events.popleft()
                await self._handle_event(event, graph, saver, thread_id)
                continue

            async with saver.async_run_lock(thread_id):
                tick = await graph.ainvoke(
                    {"state": self._state.value, "curiosity": self._curiosity,
                     "social_tick": self._social_tick,
                     "person_engaging": self._person_engaging,
                     "pending_event": {},
                     "interrupted": self.user_interrupt,
                     "pending_question": bool(self._pending_question_id),
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
                        ("event_id", "kind", "source", "content", "deadline", "activity_id")}
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
        if event.get("kind") not in {"peer_message", "teaching"}:
            return False
        decision = await self._decide_event(event)
        event["decision"] = decision
        return decision["choice"] == "accept"

    async def _advance_task(self, goal: dict, *, stage: str = "", result: dict | None = None,
                            step_done: bool = False) -> dict:
        """Persist one task transition; Drive only adapts the selected device step."""
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
        if event.get("deadline", 0) <= time.time():
            if event.get("kind") == "peer_message":
                event["messenger"].send_result(event["message_id"], "cancelled", reason="message_expired")
            return
        decision = event.get("decision") or await self._decide_event(event, graph, saver, thread_id)
        if event["kind"] == "teaching":
            if decision["choice"] != "accept" or self.user_interrupt:
                return
            current = self._identity
            from cloud.go2.perception.vision import vision_loop
            frame, _ = vision_loop.latest_frame_pair()
            if (not current or current.status != "recognized" or current.role != "owner"
                    or current.person_id != event["person_id"] or time.time() > event["deadline"]
                    or frame is None or abs(time.time() - frame.get("ts", 0)) > 5
                    or not (frame.get("faces") or {}).get("detected")):
                self._last_decision = {**decision, "reason": "identity_expired"}
                return
            if time.time() - self._action_cooldowns.get(event["action"], 0) < 30:
                self._last_decision = {**decision, "reason": "action_cooldown"}
                return
            self._active_goal = {"kind": "teaching", "event_id": event["event_id"]}
            await self._advance_task(self._active_goal, stage="start")
            result = await self._execute_motion("go2_sport", {"cmd": event["action"]})
            self._action_cooldowns[event["action"]] = time.time()
            await self._advance_task(self._active_goal, stage="finish", result=result)
            self._active_goal = None
            return
        if event["kind"] == "social":
            await self._do_social()
            return
        if event["kind"] != "peer_message":
            return
        messenger, message = event["messenger"], event["message"]
        choice = decision["choice"]
        if choice != "accept":
            if choice == "decline":
                messenger.send_result(event["message_id"], "rejected", reason="autonomous_declined")
            else:
                messenger.send_result(event["message_id"], "unknown", reason="autonomous_deferred")
            return
        task_id = "peer_" + event["message_id"]
        self._active_goal = {"kind": "peer_message", "event_id": event["event_id"],
                             "task_id": task_id, "deadline": event["deadline"]}
        messenger.send_progress(event["message_id"], task_id)
        if self._is_color_object_game(str(message.get("content") or "")):
            try:
                await self._run_color_object_game(event, task_id)
            except asyncio.CancelledError:
                messenger.send_result(event["message_id"], "cancelled", task_id=task_id,
                                      reason="autonomy_stopped")
                raise
            finally:
                self._active_goal = None
            return
        from cloud.agents.go2.graph import run_go2_loop
        from cloud.peers.inbound import _peer_input
        context = replace(self._context, expires_at=int(min(event["deadline"], time.time() + 90)))
        task = asyncio.create_task(run_go2_loop(context.session_id, _peer_input(message, messenger),
                context=context, run_id=event["message_id"], deadline=event["deadline"]))
        try:
            while not task.done():
                if self.user_interrupt or not go2.is_connected or time.time() >= event["deadline"]:
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

    @staticmethod
    def _is_color_object_game(content: str) -> bool:
        return "物品" in content and ("同色" in content or "颜色" in content)

    def _live_lamp_color(self, source: str) -> dict | None:
        from cloud.space.context import get_space_context
        from cloud.api.main import _esp32_state
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

    async def _run_color_object_game(self, event: dict, task_id: str) -> None:
        """Bounded search; descriptions remain candidates without object evidence."""
        message, messenger = event["message"], event["messenger"]
        color = self._live_lamp_color(str(event.get("source") or ""))
        if color is None:
            await self._advance_task(self._active_goal, stage="finish",
                result={"result": "rejected", "reason": "lamp_color_unknown_or_stale"})
            messenger.send_result(event["message_id"], "rejected", task_id=task_id,
                                  reason="lamp_color_unknown_or_stale")
            messenger.send_message("go2", message["sender"],
                "我没读到这盏灯的新鲜 RGB 和亮度，先不按邀请文字猜颜色。",
                reply_to=event["message_id"], reply_kind="reply")
            return
        self._active_goal = {**self._active_goal, "kind": "color_object_game",
                             "target_color": color, "deadline": event["deadline"]}
        task_state = await self._advance_task(self._active_goal, stage="start")
        observations = []
        reason = "structured_multiframe_evidence_unavailable"
        while task_state.get("next_action") == "explore_frontier":
            if self.user_interrupt or not go2.is_connected:
                reason = "autonomy_interrupted_or_offline"
                break
            if time.time() >= event["deadline"]:
                reason = "game_expired"
                break
            latest = self._live_lamp_color(color["source"])
            if latest is None:
                reason = "lamp_color_stale"
                break
            if latest["rgb"] != color["rgb"]:
                reason = "lamp_color_changed"
                break
            self._last_observation = None
            self._last_tool_result = None
            await self._do_explore(observe_question=(
                f"只描述当前画面中疑似与 RGB {color['rgb']} 同色的物品；"
                "不能把颜色描述当作已定位或已确认。"))
            if self._last_observation:
                observations.append(self._last_observation[:240])
            step_result = self._last_tool_result or {"result": "unknown"}
            task_state = await self._advance_task(self._active_goal, result=step_result,
                                                  step_done=True)
            if step_result.get("result") != "ok":
                reason = "frontier_unavailable_or_failed"
                break
        await self._advance_task(self._active_goal, stage="finish", result={"result": "unknown",
            "target_color": color, "candidate_descriptions": observations, "reason": reason})
        messenger.send_result(event["message_id"], "unknown", task_id=task_id,
                              reason=reason)
        detail = ("灯色已变化，需要重新约定目标颜色。" if reason == "lamp_color_changed" else
                  "灯色状态已过期，需要重新确认。" if reason == "lamp_color_stale" else
                  "目前只有画面文字候选，缺少跨帧物体证据，不能确认找到。")
        messenger.send_message("go2", message["sender"],
            "我按灯的实时颜色查看了安全探索点。" + detail,
            reply_to=event["message_id"], reply_kind="reply")

    async def _do_explore(self, observe_question: str = "描述当前位置看到的场景，有什么值得记录的") -> None:
        """Explore measured map frontiers; vision describes arrivals separately."""
        from cloud.go2.navigation.navigator import navigator

        if self.user_interrupt or self._pending_question_id:
            logger.info("[Drive] 探索进入即被打断")
            return

        visited = self._explore_visited
        rejected = self._explore_rejected
        logger.info("[Drive] 开始地图驱动的自主探索")
        for step in range(1):
            if self.user_interrupt or not go2.is_connected:
                break
            snapshot, reason = navigator._snapshot()
            if snapshot is None:
                logger.info("[Drive] 探索暂停：地图或定位不可用 reason=%s", reason)
                break
            odom, grid_obj = snapshot
            target = frontier_mod.find_frontier_target(grid_obj, odom, visited + rejected)
            if target is None:
                turn_check = getattr(grid_obj, "is_turn_clear", None)
                if callable(turn_check) and not turn_check(odom["x"], odom["y"]):
                    logger.info("[Drive] 探索暂停：当前转身空间不足，也没有无需转身的可达边界")
                else:
                    logger.info("[Drive] 探索暂停：当前位置没有可达的未知边界")
                break
            logger.info("[Drive] 地图边界目标 step=%d x=%.2f y=%.2f", step, *target)
            result = await self._navigate(coordinate=target)
            self._last_tool_result = result
            status = result.get("result")
            episode_memory.add(
                EventType.ACTION_TAKEN,
                f"地图边界探索步骤{step + 1}：目标({target[0]:.2f},{target[1]:.2f}) → "
                f"{self._result_text(result)}",
            )
            self._last_action_ts = time.time()
            if status != "ok":
                logger.info("[Drive] 边界导航未完成 step=%d status=%s reason=%s",
                            step, status, result.get("reason"))
                if result.get("reason") in {"turn_clearance_unavailable",
                                            "body_clearance_unavailable"}:
                    # A new goal does not change the body's current clearance.
                    # Wait for a later map/pose instead of trying more turns now.
                    break
                if status == "blocked" and len(rejected) < 3:
                    rejected.append(target)
                    break
                break
            visited.append(target)
            if len(visited) > 32:
                del visited[:-32]
            self._activity_event("action_result", self._result_text(result),
                                 action="explore_frontier", status="ok")
            # The camera reports what was reached; its availability never
            # changes the map target or authorizes movement.
            if time.time() >= self._vision_retry_after:
                try:
                    observation = await go2_observe(observe_question)
                    if observation and not observation.startswith("无可用视频帧"):
                        self._last_observation = observation
                        episode_memory.add(EventType.OBSERVATION, observation)
                        event = self._activity_event("observation", observation)
                        if event:
                            self.submit_event({"event_id": event.get("event_id", uuid.uuid4().hex),
                                "kind": "observation", "source": "go2.camera", "ts": time.time(),
                                "deadline": time.time() + 5, "activity_id": self._activity_id()})
                    else:
                        self._vision_retry_after = time.time() + 60
                except Exception as exc:
                    logger.warning("[Drive] 到达后视觉观察失败: %s", type(exc).__name__)
                    self._vision_retry_after = time.time() + 60
        logger.info("[Drive] 地图探索会话结束，已到达 %d 个目标", len(visited))

    async def _exec_explore_tool(self, tool: str, params: dict) -> str:
        """执行探索中 LLM 决策的工具调用，导航工具可被打断。"""
        self._last_execution_result = "ok"
        if self.user_interrupt or not go2.is_connected:
            self._last_execution_result = "cancelled"
            return "自主模式已中断或设备已断开"
        try:
            if tool == "explore_direction":
                return await self._exec_explore_direction(params.get("direction", "forward"))
            elif tool == "go2_sport":
                return self._result_text(await self._execute_motion("go2_sport", {"cmd": params.get("cmd", "")}))
            elif tool == "go2_observe":
                return await go2_observe(params.get("question", "描述当前场景"))
            elif tool == "go2_navigate_to":
                name = params.get("name", "")
                return self._result_text(await self._navigate(name))
            elif tool == "go2_tag_location":
                from cloud.agents.go2.memory import spatial as spatial_memory
                odom = go2.odom
                if (not odom or getattr(go2, "odom_received_at", None) is None or
                        time.monotonic() - go2.odom_received_at > 1.0 or
                        getattr(go2, "odom_frame_id", None) != "odom"):
                    return "无法标记：odom 数据不可用"
                return spatial_memory.tag_location(params.get("name", "unknown"), odom)
            elif tool == "send_message":
                from cloud.space.context import get_space_context
                from cloud.api.main import _esp32_state
                view = get_space_context("go2", state=_esp32_state)
                activity, peers = activity_peers("go2", view["peers"])
                target = str(params.get("target") or "")
                content = str(params.get("content") or "")
                if (not activity or target not in {peer["unit_id"] for peer in peers}
                        or not self._social_state.can_send(target, content)):
                    self._last_execution_result = "blocked"
                    return "当前活动或社交预算不允许发信"
                from cloud.go2.tools.tools import send_message
                outcome = send_message(target, content)
                status = outcome.get("result", "error")
                if status == "accepted":
                    self._social_state.sent(target, content)
                self._last_execution_result = "ok" if status == "accepted" else status
                return ("消息已发送，尚未确认同伴回复" if status == "accepted"
                        else self._result_text(outcome))
            elif tool == "ask_user":
                activity_id = self._activity_id()
                question = str(params.get("question") or "").strip()[:400]
                if not activity_id or not question:
                    self._last_execution_result = "blocked"
                    return "问题不可用"
                from cloud.go2.activity_media import save_snapshot
                question_id = "q_" + uuid.uuid4().hex
                media_ref = save_snapshot(activity_id, question_id, go2._latest_frame)
                self._activity_event("question", question, question_id=question_id,
                                     media_ref=media_ref)
                self._pending_question_id = question_id
                episode_memory.add(EventType.OBSERVATION, f"向用户提问：{question}")
                self._last_execution_result = "paused"
                return "等待用户回答；探索已暂停"
            else:
                return f"未知工具: {tool}"
        except Exception as exc:
            return f"执行失败: {exc}"

    async def _return_home(self) -> None:
        """探索结束后导航回 home 点，被打断时就地停止。"""
        logger.info("[Drive] 探索结束，导航回 home")
        result = await self._navigate("home")
        logger.info("[Drive] 回家导航结果：%s", result)

    async def _exec_explore_direction(self, direction: str) -> str:
        """explore_direction 工具的实现：用地图算出安全目标坐标，Navigator 闭环导过去。

        地图不可用或无已知安全路线时拒绝移动。
        """
        from cloud.go2.navigation.navigator import navigator

        odom = go2.odom
        if not odom:
            return "无法移动：odom 不可用"

        trusted_map, map_reason = navigator._snapshot()
        if trusted_map is None:
            self._last_execution_result = "blocked"
            return f"地图或定位未就绪（{map_reason}），不能安全探索该方向"
        odom, grid_obj = trusted_map
        target = frontier_mod.find_exploration_target(grid_obj, odom, direction)

        if grid_obj is not None and target is None:
            self._last_execution_result = "blocked"
            return (
                f"方向「{direction}」路径不通，不能向前移动。"
                "建议先远观并记录观察，再选择地图中的可通行方向。"
            )

        target_x, target_y = target

        # A* 可达性预检查：射线通畅不等于路径可规划，提前排除障碍包围的目的地
        from cloud.go2.navigation.astar import astar
        from cloud.go2.navigation.navigator import _nearest_free_cell
        try:
            start_cell = grid_obj.odom_to_grid(odom["x"], odom["y"])
            goal_raw = grid_obj.odom_to_grid(target_x, target_y)
        except ValueError:
            self._last_execution_result = "blocked"
            return "探索目标超出当前地图，不能移动"
        goal_cell  = _nearest_free_cell(grid_obj, goal_raw, max_r=20)
        if goal_cell is None or not astar(grid_obj.grid, start_cell, goal_cell):
            logger.info("[Drive] explore_direction=%s A*预检查不通，引导远观绕行", direction)
            return (
                f"方向「{direction}」路径不通，走不过去。"
                f"建议：用 go2_observe 远观该方向的兴趣点并记录观察，"
                f"然后从★开阔或○可通行方向继续探索。"
            )

        nav_result = await self._navigate(coordinate=(target_x, target_y))
        if nav_result.get("result") != "ok":
            return self._result_text(nav_result)
        result = self._result_text(nav_result)

        new_odom = go2.odom
        if new_odom:
            dx   = new_odom["x"] - odom["x"]
            dy   = new_odom["y"] - odom["y"]
            dist = (dx ** 2 + dy ** 2) ** 0.5
            nav_summary = (
                f"{result}，实际移动 {dist:.2f}m，"
                f"当前位置 ({new_odom['x']:.2f}, {new_odom['y']:.2f})"
            )
        else:
            nav_summary = result

        observation = await go2_observe("描述当前看到的场景，有什么值得继续探索的")
        observed_event = self._activity_event("observation", observation)
        await self._evaluate_social(observed_event)
        return f"{nav_summary} | 到达后观察：{observation}"

    async def _do_social(self) -> None:
        """Social opportunity uses the same bounded event handling checkpoint."""
        await self._evaluate_social()

    # ── 生命周期 ───────────────────────────────────────────────────

    def start(self, owner_session_id: str = "system", context: ExecutionContext | None = None) -> None:
        """启动内驱动循环，重置所有状态，创建后台任务。"""
        if self._task is not None:
            self._task.cancel()
        self._state            = MotivationalState.IDLE
        self._curiosity        = 0
        self._person_present   = False
        self._person_engaging  = False
        self._task             = asyncio.create_task(self._run_loop())
        self._loop = asyncio.get_running_loop()
        self._generation += 1
        self._events.clear()
        self._seen_events.clear()
        self._explore_visited.clear()
        self._explore_rejected.clear()
        self.owner_session_id  = owner_session_id
        self._context = replace(context or SYSTEM_CONTEXT, origin_kind="agent_autonomous")
        self.user_interrupt = False
        self._pending_question_id = None
        logger.info("[Drive] 内驱动循环已启动")

    def stop(self, owner_session_id: str | None = None) -> bool:
        """停止内驱动循环，取消后台任务，同时停止 navigator 避免孤儿导航 task。"""
        if owner_session_id is not None and self.owner_session_id != owner_session_id:
            return False
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
