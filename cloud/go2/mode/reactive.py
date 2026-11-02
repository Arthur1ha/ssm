import asyncio
import logging
import time
import uuid
from dataclasses import replace
from typing import Optional

from cloud.go2.perception.vision import VisionFrame
from cloud.go2.tools.tools import get_text_llm, _VALID_SPORT_CMDS
from cloud.go2.tools.executor import execute_tool
from cloud.go2.execution import ExecutionContext, SYSTEM_CONTEXT
from cloud.agents.go2.personality.profile import get_profile
from cloud.agents.go2.memory.episode import episode_memory, EventType
from cloud.go2.space_context import prompt_space_context

logger = logging.getLogger(__name__)

_AUTONOMY_COOLDOWN_S = 15   # 两次自主行为之间的最小间隔
_ACTION_COOLDOWN_S   = 30   # 同一动作的默认冷却

# 遥控模式下的即时反应动作：只做姿态和转向，不走动
_TURN_ACTIONS: dict[str, dict] = {
    "turn_left":  {"direction": "turn_left"},
    "turn_right": {"direction": "turn_right"},
}

_ALL_ACTIONS = sorted(_VALID_SPORT_CMDS) + sorted(_TURN_ACTIONS)


def _frame_to_observation(frame: VisionFrame) -> str:
    parts = []
    if frame["persons"]["detected"]:
        parts.append("人")
    if frame["faces"]["detected"]:
        parts.append("脸")
    return "，".join(parts)


class ReactiveMind:
    def __init__(self):
        self._last_decision: Optional[dict] = None
        self._last_autonomous_ts: float = 0.0
        # 自主动作冷却：action → last_triggered_ts
        self._action_cooldowns: dict[str, float] = {}
        # 自主层自己跟踪人/脸数量，跨帧变化即视为"画面有变化"。
        # 不复用 frame["changed"]——后者只看人数，且 HOG 全身在近距离常漏检，
        # 人脸（Haar 正脸）在近距离更可靠，必须纳入触发。-1 表示尚无基线。
        self._prev_person_count: int = -1
        self._prev_face_count: int = -1
        self._identity = None
        self._active = False
        self._generation = 0
        self._context: ExecutionContext = SYSTEM_CONTEXT
        self._last_prompt_metadata: dict | None = None
        self._reason_lock = asyncio.Lock()

    def set_active(self, active: bool, context: ExecutionContext | None = None) -> None:
        self._generation += 1
        self._active = active
        if context is not None:
            self._context = replace(context, origin_kind="agent_autonomous")

    @property
    def last_decision(self) -> Optional[dict]:
        return self._last_decision

    def _in_action_cooldown(self, action: str) -> bool:
        return time.time() - self._action_cooldowns.get(action, 0) < _ACTION_COOLDOWN_S

    def on_identity_event(self, event) -> None:
        """身份变化可作为已声明的观察；动作仍由自主 Graph 决策。"""
        self._identity = event.current
        current, previous = event.current, event.previous
        if (not self._active or current.status != "recognized" or current.role != "owner"
                or previous.status == "recognized" and previous.person_id == current.person_id):
            return
        from cloud.go2.perception.vision import vision_loop
        frame, _ = vision_loop.latest_frame_pair()
        if frame is None or abs(time.time() - frame["ts"]) > 5:
            return
        observation = {"id": "vision.owner_recognized", "values": {"recognized": True},
                       "source": "go2.identity", "ts": current.ts,
                       "person_id": current.person_id}
        try:
            asyncio.get_running_loop().create_task(
                self._autonomous_reason(frame, self._generation, identity_observation=observation))
        except RuntimeError:
            return

    def _identity_context(self) -> str:
        current = self._identity
        if current and current.status == "recognized":
            return f"当前人物：{current.name}（{current.role}，置信分 {current.score:.1f}）"
        if current and current.status == "unknown":
            return "当前人物：陌生人"
        return "当前人物：未确认"

    # ── 自主动作执行口 ────────────────────────────

    async def _dispatch_action(self, action: str, reason: str, source: str, generation: int | None = None) -> None:
        """真正下发一个动作：校验 → 共享冷却 → 执行 → 记忆 → 日志。"""
        if not self._active or (generation is not None and generation != self._generation):
            return
        if action not in _ALL_ACTIONS:
            logger.info("[ReactiveMind] 未知动作 %s，跳过", action)
            return
        if self._in_action_cooldown(action):
            logger.info("[ReactiveMind] %s 冷却中，跳过（%s）", action, source)
            return
        try:
            if action in _VALID_SPORT_CMDS:
                result = await execute_tool("go2_sport", {"cmd": action}, context=self._context)
            else:
                result = await execute_tool("go2_move", _TURN_ACTIONS[action], context=self._context)
            if self._active and (generation is None or generation == self._generation):
                asyncio.create_task(self._publish_action_reply(action, reason, result))
            if result.get("result") != "ok":
                self._action_cooldowns[action] = time.time()
                return
        except Exception as exc:
            logger.warning("[ReactiveMind] 执行失败: %s", exc)
            if self._active and (generation is None or generation == self._generation):
                asyncio.create_task(self._publish_action_reply(
                    action, reason, {"result": "error", "error": type(exc).__name__}))
            return
        self._action_cooldowns[action] = time.time()
        episode_memory.add(EventType.ACTION_TAKEN, f"{source}：执行了 {action}（{reason}）")
        logger.info("[ReactiveMind] %s 执行 %s：%s", source, action, reason)

    async def _publish_action_reply(self, action: str, reason: str, result: dict) -> None:
        from cloud.agents.go2.workflows.reply import run_autonomous_reply_graph
        from cloud.api.intents import publish_agent_event
        from cloud.go2.connection import go2

        if not go2.is_connected:
            return
        run_id = "auto_" + uuid.uuid4().hex
        status = str(result.get("result") or "unknown")
        publish_agent_event("agent_progress", agent_id="go2", status=status,
            scope="space", space_id=self._context.space_id, run_id=run_id,
            step_id=run_id, source="autonomous", stage="observed")
        answer = await run_autonomous_reply_graph(
            run_id=run_id, action=action, reason=reason,
            params={"reason": reason}, result=result,
            model=get_text_llm(), profile_loader=get_profile)
        if not self._active or not go2.is_connected:
            return
        publish_agent_event("agent_reply", agent_id="go2", status=status,
            scope="space", space_id=self._context.space_id, run_id=run_id,
            source="autonomous", text=answer)

    # ── 同步入口，由 vision_loop 回调 ─────────────────────────────

    def on_vision_frame(self, frame: VisionFrame) -> None:
        if not self._active:
            return
        # 人数或脸数发生跨帧变化即视为"画面有变化"（首帧建立基线不算）
        person_count = frame["persons"]["count"]
        face_count   = frame["faces"]["count"]
        changed = (
            (self._prev_person_count >= 0 and person_count != self._prev_person_count)
            or (self._prev_face_count >= 0 and face_count != self._prev_face_count)
        )
        self._prev_person_count = person_count
        self._prev_face_count   = face_count

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return

        # Visual changes can still prompt the agent; remembered teachings are inert.
        if changed:
            loop.create_task(self._autonomous_reason(frame, self._generation))

    # ── 异步自主推理 ──────────────────────────────────────────────

    async def _autonomous_reason(self, frame: VisionFrame, generation: int | None = None,
                                 identity_observation: dict | None = None) -> None:
        async with self._reason_lock:
            await self._reason_once(frame, generation, identity_observation)

    async def _reason_once(self, frame: VisionFrame, generation: int | None,
                           identity_observation: dict | None) -> None:
        """Project a vision change into Go2's checkpointed autonomous Graph."""
        if not self._active or (generation is not None and generation != self._generation):
            return
        from cloud.agents.common.checkpoint import FileCheckpointSaver
        from cloud.agents.go2.workflows.reactive import build_action_graph
        from cloud.api.intents import publish_agent_event
        from cloud.go2.connection import go2

        now = time.time()
        scene = (f"{frame['change_type']}；"
                 f"{frame['persons']['count']} 人，{frame['faces']['count']} 张脸")
        if identity_observation:
            current = self._identity
            if (current.status == "recognized" and current.role == "owner"
                    and current.person_id == identity_observation.get("person_id")):
                identity_observation = {**identity_observation, "ts": now}
            else:
                identity_observation = None
        if identity_observation:
            scene += "；已识别主人"
        from cloud.agents.common import teachings
        from cloud.agents.common.teaching_bindings import candidates
        from cloud.cards.registry import get_registry
        card = get_registry().get_card("go2")
        teaching_candidates = []
        if card:
            records = teachings.list_for("go2", space_id=self._context.space_id)
            for kind, detected in (("person", frame["persons"]["detected"]),
                                   ("face", frame["faces"]["detected"])):
                observation = {"id": f"vision.{kind}_presence", "values": {"detected": detected},
                               "source": "go2.camera", "ts": frame["ts"], "frame_id": frame["frame_id"]}
                teaching_candidates.extend(candidates(records, observation, card, now=now))
            if identity_observation:
                teaching_candidates.extend(candidates(records, identity_observation, card, now=now))
        run_id = "auto_" + uuid.uuid4().hex
        def live() -> bool:
            return (self._active and go2.is_connected and
                    (generation is None or generation == self._generation))
        async def execute(action: str, reason: str) -> dict:
            if not live() or self._in_action_cooldown(action):
                return {"result": "blocked", "error": "autonomy_gate_changed"}
            if action in _VALID_SPORT_CMDS:
                result = await execute_tool("go2_sport", {"cmd": action}, context=self._context)
            elif action in _TURN_ACTIONS:
                result = await execute_tool("go2_move", _TURN_ACTIONS[action], context=self._context)
            else:
                return {"result": "rejected", "error": "unknown_action"}
            if result.get("result") == "ok":
                episode_memory.add(EventType.ACTION_TAKEN, f"自主响应：执行了 {action}（{reason}）")
            return result
        saver = FileCheckpointSaver()
        graph = build_action_graph(
            get_text_llm(), kind="reactive", profile_loader=get_profile,
            execute=execute, can_dispatch=live, checkpointer=saver)
        thread_id = "go2:reactive:" + run_id
        state = {"run_id": run_id, "kind": "reactive", "scene": scene,
                 "identity": self._identity_context(),
                 "memory_summary": episode_memory.format_context(),
                 "space_summary": prompt_space_context(),
                 "active": self._active, "connected": go2.is_connected,
                 "interrupted": False, "now": now,
                 "last_autonomous_ts": self._last_autonomous_ts,
                 "teaching_candidates": teaching_candidates,
                 "action_cooldowns": dict(self._action_cooldowns),
                 "last_decision": self._last_decision}
        async with saver.async_run_lock(thread_id):
            result = await graph.ainvoke(state, {"configurable": {"thread_id": thread_id}},
                                         durability="sync")
        self._last_autonomous_ts = result.get("last_autonomous_ts", self._last_autonomous_ts)
        self._last_decision = result.get("last_decision", self._last_decision)
        self._action_cooldowns.update(result.get("action_cooldowns") or {})
        if result.get("result") and result.get("decision", {}).get("action") and live():
            status = result["result"].get("result", "unknown")
            publish_agent_event("agent_progress", agent_id="go2", status=status,
                scope="space", space_id=self._context.space_id, run_id=run_id,
                step_id=run_id, source="autonomous", stage="observed")
            if result.get("response"):
                publish_agent_event("agent_reply", agent_id="go2", status=status,
                    scope="space", space_id=self._context.space_id, run_id=run_id,
                    source="autonomous", text=result["response"])


reactive_mind = ReactiveMind()
