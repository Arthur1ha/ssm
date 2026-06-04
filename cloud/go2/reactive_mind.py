import asyncio
import json
import logging
import re
import time
from typing import Optional

from langchain_core.messages import SystemMessage, HumanMessage

from cloud.go2.vision import VisionFrame
from cloud.go2.tools import check_rules, go2_sport, go2_move, get_text_llm, _VALID_SPORT_CMDS
from cloud.go2.personality import get_system_prompt
from cloud.go2.episode_memory import episode_memory, EventType

logger = logging.getLogger(__name__)

_AUTONOMY_COOLDOWN_S = 15   # 两次自主行为之间的最小间隔
_ACTION_COOLDOWN_S   = 30   # 同一动作的默认冷却

# 移动动作：action key → go2_move 参数
_MOVE_ACTIONS: dict[str, dict] = {
    "move_forward":  {"direction": "forward"},
    "move_backward": {"direction": "backward"},
    "move_left":     {"direction": "left"},
    "move_right":    {"direction": "right"},
    "turn_left":     {"direction": "turn_left"},
    "turn_right":    {"direction": "turn_right"},
}

_ALL_ACTIONS = sorted(_VALID_SPORT_CMDS) + sorted(_MOVE_ACTIONS)


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

    @property
    def last_decision(self) -> Optional[dict]:
        return self._last_decision

    def _in_action_cooldown(self, action: str) -> bool:
        return time.time() - self._action_cooldowns.get(action, 0) < _ACTION_COOLDOWN_S

    # ── 同步入口，由 vision_loop 回调 ─────────────────────────────

    def on_vision_frame(self, frame: VisionFrame) -> None:
        observation = _frame_to_observation(frame)
        rule_actions = check_rules(observation) if observation else []

        # 规则触发的动作同步写入共享冷却
        now = time.time()
        for action in rule_actions:
            self._action_cooldowns[action] = now

        # 只有 changed=True 且规则层没有触发时，才启动自主推理
        if frame["changed"] and not rule_actions:
            try:
                loop = asyncio.get_running_loop()
                loop.create_task(self._autonomous_reason(frame))
            except RuntimeError:
                pass

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
            f"  移动类：{', '.join(sorted(_MOVE_ACTIONS))}（移动持续约 1s）\n\n"
            f"决定你想做什么（或什么都不做）。\n"
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

        if action and action in _ALL_ACTIONS:
            if self._in_action_cooldown(action):
                logger.info("[ReactiveMind] %s 冷却中，跳过", action)
                self._last_autonomous_ts = now
                return
            try:
                if action in _VALID_SPORT_CMDS:
                    await go2_sport(action)
                else:
                    await go2_move(**_MOVE_ACTIONS[action])
                self._action_cooldowns[action] = time.time()
                self._last_autonomous_ts = time.time()
                episode_memory.add(EventType.ACTION_TAKEN, f"自主响应：执行了 {action}（{reason}）")
                logger.info("[ReactiveMind] 自主执行 %s：%s", action, reason)
            except Exception as exc:
                logger.warning("[ReactiveMind] 执行失败: %s", exc)
        else:
            self._last_autonomous_ts = now
            logger.info("[ReactiveMind] 决定不行动：%s", reason)


reactive_mind = ReactiveMind()
