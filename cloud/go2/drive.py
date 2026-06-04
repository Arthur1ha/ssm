# cloud/go2/drive.py
import asyncio
import json
import logging
import re
import time
from enum import Enum
from typing import Optional

from langchain_core.messages import SystemMessage, HumanMessage

from cloud.go2.tools import go2_sport, go2_move, go2_observe, get_text_llm, _VALID_SPORT_CMDS
from cloud.go2.vision import VisionFrame
from cloud.go2.personality import get_system_prompt
from cloud.go2.episode_memory import episode_memory, EventType

logger = logging.getLogger(__name__)

_CURIOSITY_THRESHOLD   = 120  # 2 分钟无事件 → EXPLORING
_PERSON_GONE_TIMEOUT   = 30   # 人消失 30s → 回 IDLE
_SOCIAL_CHECK_INTERVAL = 30   # SOCIAL 状态下每 30s 主动检查

_EXPLORE_MOVES: dict[str, dict] = {
    "turn_left":    {"direction": "turn_left"},
    "turn_right":   {"direction": "turn_right"},
    "move_forward": {"direction": "forward"},
}


class MotivationalState(str, Enum):
    IDLE      = "IDLE"
    ALERT     = "ALERT"
    SOCIAL    = "SOCIAL"
    EXPLORING = "EXPLORING"


class Drive:
    def __init__(self) -> None:
        self._state          = MotivationalState.IDLE
        self._curiosity      = 0
        self._person_present = False
        self._last_person_ts = 0.0
        self._last_action_ts = 0.0
        self._social_tick    = 0
        self.user_interrupt  = False
        self._task: Optional[asyncio.Task] = None

    @property
    def state_snapshot(self) -> dict:
        return {
            "state":          self._state.value,
            "curiosity":      self._curiosity,
            "person_present": self._person_present,
            "last_action_ts": self._last_action_ts,
        }

    # ── Vision 回调 ────────────────────────────────────────────────

    def on_vision_frame(self, frame: VisionFrame) -> None:
        currently_present = frame["persons"]["detected"]

        if currently_present and not self._person_present:
            self._person_present = True
            self._curiosity = 0
            if self._state == MotivationalState.IDLE:
                self._state = MotivationalState.SOCIAL
                self._social_tick = 0
            episode_memory.add(
                EventType.VISION_CHANGE,
                f"检测到 {frame['persons']['count']} 人进入画面",
            )
        elif not currently_present and self._person_present:
            self._person_present = False
            self._last_person_ts = time.time()
            episode_memory.add(EventType.VISION_CHANGE, "画面中的人已离开")
        elif frame["changed"] and frame["change_type"] != "none":
            episode_memory.add(EventType.VISION_CHANGE, f"视觉变化：{frame['change_type']}")

    # ── 主循环 ─────────────────────────────────────────────────────

    async def _run_loop(self) -> None:
        while True:
            await asyncio.sleep(1)
            if self.user_interrupt:
                continue

            self._curiosity += 1
            self._social_tick += 1

            if self._state == MotivationalState.SOCIAL:
                if not self._person_present:
                    if time.time() - self._last_person_ts >= _PERSON_GONE_TIMEOUT:
                        self._state = MotivationalState.IDLE
                        self._curiosity = 0
                elif self._social_tick >= _SOCIAL_CHECK_INTERVAL:
                    self._social_tick = 0
                    await self._do_social()

            elif self._state == MotivationalState.IDLE:
                if self._curiosity >= _CURIOSITY_THRESHOLD:
                    self._state = MotivationalState.EXPLORING
                    self._curiosity = 0
                    await self._do_explore()
                    self._state = MotivationalState.IDLE

    async def _do_explore(self) -> None:
        memory_ctx = episode_memory.format_context()
        prompt = (
            f"{memory_ctx}\n\n"
            f"你现在感到无聊，想主动探索一下环境。\n"
            f"可用移动动作：{', '.join(_EXPLORE_MOVES)}\n"
            f"可用姿态动作：{', '.join(sorted(_VALID_SPORT_CMDS))}\n\n"
            f"选择一个探索行为（转向观察或移动一小步）。\n"
            f"直接输出 JSON，不含代码块：{{\"action\": \"动作名\", \"reason\": \"一句话\"}}"
        )
        try:
            resp = await get_text_llm().ainvoke([
                SystemMessage(content=get_system_prompt()),
                HumanMessage(content=prompt),
            ])
            content = re.sub(r"```(?:json)?\n?", "", resp.content.strip()).strip().rstrip("`")
            decision = json.loads(content)
        except Exception as exc:
            logger.warning("[Drive] 探索推理失败: %s", exc)
            return

        action = decision.get("action")
        reason = decision.get("reason", "")
        if not action:
            return

        try:
            if action in _VALID_SPORT_CMDS:
                await go2_sport(action)
            elif action in _EXPLORE_MOVES:
                await go2_move(**_EXPLORE_MOVES[action])
            else:
                return
            self._last_action_ts = time.time()
            episode_memory.add(EventType.ACTION_TAKEN, f"自主探索：执行了 {action}（{reason}）")
            logger.info("[Drive] EXPLORING 执行 %s：%s", action, reason)
        except Exception as exc:
            logger.warning("[Drive] 探索执行失败: %s", exc)

    async def _do_social(self) -> None:
        try:
            observation = await go2_observe("描述画面中的人在做什么")
        except Exception as exc:
            logger.warning("[Drive] observe 失败: %s", exc)
            return

        episode_memory.add(EventType.OBSERVATION, f"社交观察：{observation}")
        memory_ctx = episode_memory.format_context()
        prompt = (
            f"{memory_ctx}\n\n"
            f"最新观察：{observation}\n"
            f"可用姿态动作：{', '.join(sorted(_VALID_SPORT_CMDS))}\n\n"
            f"决定是否对这个人做出互动（或什么都不做）。\n"
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
            logger.warning("[Drive] 社交推理失败: %s", exc)
            return

        action = decision.get("action")
        reason = decision.get("reason", "")
        if action and action in _VALID_SPORT_CMDS:
            try:
                await go2_sport(action)
                self._last_action_ts = time.time()
                episode_memory.add(EventType.ACTION_TAKEN, f"社交互动：执行了 {action}（{reason}）")
                logger.info("[Drive] SOCIAL 执行 %s：%s", action, reason)
            except Exception as exc:
                logger.warning("[Drive] 社交执行失败: %s", exc)
        else:
            logger.info("[Drive] SOCIAL 决定不互动：%s", reason)

    # ── 生命周期 ───────────────────────────────────────────────────

    def start(self) -> None:
        if self._task is not None:
            self._task.cancel()
        self._state          = MotivationalState.IDLE
        self._curiosity      = 0
        self._person_present = False
        self._task           = asyncio.create_task(self._run_loop())
        logger.info("[Drive] 内驱动循环已启动")

    def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            self._task = None
        logger.info("[Drive] 内驱动循环已停止")


drive = Drive()
