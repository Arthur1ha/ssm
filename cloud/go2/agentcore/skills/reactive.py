import asyncio
import json
import logging
import re
import time
from typing import Optional

from langchain_core.messages import SystemMessage, HumanMessage

from cloud.go2.agentcore.skills.vision import VisionFrame
from cloud.go2.agentcore.tools.tools import check_rules, go2_sport, go2_move, get_text_llm, _VALID_SPORT_CMDS
from cloud.go2.agentcore.soul import get_system_prompt
from cloud.go2.agentcore.memory.episode import episode_memory, EventType

logger = logging.getLogger(__name__)

_THOUGHT_TOPIC = "ssm/agents/go2/thought"


def _publish_thought(payload: dict) -> None:
    from cloud.api.main import get_mqtt_client
    client = get_mqtt_client()
    if client:
        client.publish(_THOUGHT_TOPIC, json.dumps(payload, ensure_ascii=False))


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
        # 规则层和自主层共用同一份冷却：action → last_triggered_ts
        self._action_cooldowns: dict[str, float] = {}
        # 自主层自己跟踪人/脸数量，跨帧变化即视为"画面有变化"。
        # 不复用 frame["changed"]——后者只看人数，且 HOG 全身在近距离常漏检，
        # 人脸（Haar 正脸）在近距离更可靠，必须纳入触发。-1 表示尚无基线。
        self._prev_person_count: int = -1
        self._prev_face_count: int = -1

    @property
    def last_decision(self) -> Optional[dict]:
        return self._last_decision

    def _in_action_cooldown(self, action: str) -> bool:
        return time.time() - self._action_cooldowns.get(action, 0) < _ACTION_COOLDOWN_S

    # ── 单一执行口，规则层与自主层共用 ────────────────────────────

    async def _dispatch_action(self, action: str, reason: str, source: str) -> None:
        """真正下发一个动作：校验 → 共享冷却 → 执行 → 记忆 → 日志。"""
        if action not in _ALL_ACTIONS:
            logger.info("[ReactiveMind] 未知动作 %s，跳过", action)
            return
        if self._in_action_cooldown(action):
            logger.info("[ReactiveMind] %s 冷却中，跳过（%s）", action, source)
            return
        try:
            if action in _VALID_SPORT_CMDS:
                await go2_sport(action)
            else:
                await go2_move(**_TURN_ACTIONS[action])
        except Exception as exc:
            logger.warning("[ReactiveMind] 执行失败: %s", exc)
            _publish_thought({"type": "act", "text": f"尝试执行{action}，但失败了：{exc}"})
            return
        self._action_cooldowns[action] = time.time()
        episode_memory.add(EventType.ACTION_TAKEN, f"{source}：执行了 {action}（{reason}）")
        logger.info("[ReactiveMind] %s 执行 %s：%s", source, action, reason)
        _publish_thought({"type": "act", "text": f"执行了{action}"})

    # ── 同步入口，由 vision_loop 回调 ─────────────────────────────

    def on_vision_frame(self, frame: VisionFrame) -> None:
        observation = _frame_to_observation(frame)
        rule_actions = check_rules(observation) if observation else []

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

        # 规则层（快速反射）优先：命中即执行，不再走 LLM 推理
        if rule_actions:
            for action in rule_actions:
                loop.create_task(self._dispatch_action(action, "规则命中", "规则响应"))
            return

        # 规则未命中且画面变化 → 自主推理（LLM 兜底）
        if changed:
            loop.create_task(self._autonomous_reason(frame))

    # ── 异步自主推理 ──────────────────────────────────────────────

    async def _autonomous_reason(self, frame: VisionFrame) -> None:
        now = time.time()
        if now - self._last_autonomous_ts < _AUTONOMY_COOLDOWN_S:
            return

        scene_parts = []
        if frame["persons"]["detected"]:
            scene_parts.append(f"{frame['persons']['count']} 人")
        if frame["faces"]["detected"]:
            scene_parts.append(f"{frame['faces']['count']} 张脸")
        scene = "，".join(scene_parts) if scene_parts else "没有人"

        cooldown_lines = [
            f"{a}（还需 {max(0, int(_ACTION_COOLDOWN_S - (now - ts)))}s）"
            for a, ts in self._action_cooldowns.items()
            if now - ts < _ACTION_COOLDOWN_S
        ]
        cooldown_str = "、".join(cooldown_lines) if cooldown_lines else "无"

        last_str = (
            f"{self._last_decision['action']}（{self._last_decision['reason']}）"
            if self._last_decision and self._last_decision.get("action")
            else "无"
        )

        memory_ctx = episode_memory.format_context()
        prompt = (
            f"{memory_ctx}\n\n"
            f"当前画面变化：{frame['change_type']}，场景：{scene}。\n"
            f"规则层没有触发任何动作。\n"
            f"上次自主动作：{last_str}\n"
            f"冷却中的动作：{cooldown_str}\n"
            f"可用动作：\n"
            f"  姿态类：{', '.join(sorted(_VALID_SPORT_CMDS))}\n"
            f"  转向类：{', '.join(sorted(_TURN_ACTIONS))}（用于朝向变化方向）\n\n"
            f"决定你想做什么（或什么都不做）。不要走动，只做姿态或转向。\n"
            f"直接输出 JSON，不含代码块：{{\"action\": \"动作名或null\", \"reason\": \"一句话\"}}"
        )

        try:
            resp = await get_text_llm().ainvoke([
                SystemMessage(content=get_system_prompt()),
                HumanMessage(content=prompt),
            ])
            content = re.sub(r"```(?:json)?\n?", "", resp.content.strip()).strip().rstrip("`")
            decision = json.loads(content)
        except Exception as exc:
            logger.warning("[ReactiveMind] LLM 解析失败: %s", exc)
            return

        action = decision.get("action")
        reason = decision.get("reason", "")

        self._last_decision = {
            "action":       action,
            "reason":       reason,
            "ts":           now,
            "frame_change": frame["change_type"],
        }
        self._last_autonomous_ts = now

        _publish_thought({"type": "think", "text": reason})

        if action:
            await self._dispatch_action(action, reason, "自主响应")
        else:
            logger.info("[ReactiveMind] 决定不行动：%s", reason)


reactive_mind = ReactiveMind()
