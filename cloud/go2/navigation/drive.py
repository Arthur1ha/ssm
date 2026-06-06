"""
机器人内驱动系统，根据视觉感知自主切换行为状态。
状态机：IDLE → SOCIAL（检测到人）/ EXPLORING（长时间无事件）。
SOCIAL 状态下每 8s 观察并决策是否互动；EXPLORING 状态下 LLM 驱动最多 15 步自主探索。
"""
import asyncio
import json
import logging
import re
import time
from enum import Enum
from typing import Optional

from langchain_core.messages import SystemMessage, HumanMessage

from cloud.go2.connection import go2
from cloud.go2.agentcore.tools.tools import go2_sport, go2_observe, get_text_llm, _VALID_SPORT_CMDS
from cloud.go2.agentcore.skills.vision import VisionFrame
from cloud.go2.agentcore.soul import get_system_prompt
from cloud.go2.agentcore.memory.episode import episode_memory, EventType
from cloud.go2.navigation import frontier as frontier_mod

logger = logging.getLogger(__name__)

_CURIOSITY_THRESHOLD   = 30  # 半分钟无事件 → EXPLORING
_PERSON_GONE_TIMEOUT   = 30   # 人消失 30s → 回 IDLE
_SOCIAL_CHECK_INTERVAL = 8    # SOCIAL 状态下每 8s 主动检查


class MotivationalState(str, Enum):
    IDLE      = "IDLE"
    ALERT     = "ALERT"
    SOCIAL    = "SOCIAL"
    EXPLORING = "EXPLORING"


class Drive:
    """内驱动控制器，管理动机状态机和自主行为触发。"""

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
        """处理视觉帧：人出现时切 SOCIAL，人消失时记时，场景变化时写 episode 记忆。"""
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
        """主驱动循环，每秒推进好奇心计数并根据状态触发对应行为。"""
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
        """EXPLORING 状态：LLM 驱动的多步自主探索，最多 15 步，被人打断或用户打断时提前结束。"""
        _MAX_STEPS = 15
        history: list[dict] = []
        logger.info("[Drive] 开始自主探索会话")

        for step in range(_MAX_STEPS):
            if self.user_interrupt or self._person_present:
                logger.info("[Drive] EXPLORING 被打断 step=%d", step)
                break

            from cloud.go2.agentcore.memory import spatial as spatial_memory
            odom = go2.odom
            odom_str = (
                f"x={odom['x']:.2f}, y={odom['y']:.2f}, heading={odom['heading']:.2f}rad"
                if odom else "未知"
            )
            locations = spatial_memory.list_locations()
            loc_str = ", ".join(l["name"] for l in locations) if locations else "无"
            history_str = "\n".join(
                f"  步骤{h['step']}: [{h['tool']}] {h['reason']} → {h['result']}"
                for h in history
            ) or "  （尚未执行任何步骤）"

            # 射线投射：告诉 LLM 各方向实际可行距离，让方向选择有依据
            grid_obj = go2.occupancy_grid
            if grid_obj and odom:
                dir_free = frontier_mod.raycast_directions(grid_obj, odom)
                dir_str  = "  ".join(f"{d}:{v:.1f}m" for d, v in dir_free.items())
            else:
                dir_str = "地图未就绪"

            prompt = (
                f"{episode_memory.format_context()}\n\n"
                f"你正在自主探索环境，当前第 {step + 1} 步（最多 {_MAX_STEPS} 步）。\n"
                f"当前位置：{odom_str}\n"
                f"已知地点：{loc_str}\n"
                f"各方向可行空间：{dir_str}\n\n"
                f"本次探索历史：\n{history_str}\n\n"
                f"可用工具：\n"
                f"  explore_direction → direction: {'/'.join(frontier_mod.DIRECTIONS)}\n"
                f"                      选择想去的方向，系统根据地图自动算出安全目标并导航过去\n"
                f"  go2_sport         → cmd: {', '.join(sorted(_VALID_SPORT_CMDS))}\n"
                f"  go2_observe       → question: 想观察什么\n"
                f"  navigator_go      → name: 导航到已知地点\n"
                f"  tag_location      → name: 给当前位置命名并保存\n"
                f"  stop              → 结束探索（累了/满足了/想休息）\n\n"
                f"直接输出 JSON，不含代码块：\n"
                f"{{\"tool\": \"工具名\", \"params\": {{...}}, \"reason\": \"一句话\", \"done\": false}}"
            )

            try:
                resp = await get_text_llm().ainvoke([
                    SystemMessage(content=get_system_prompt()),
                    HumanMessage(content=prompt),
                ])
                content = re.sub(r"```(?:json)?\n?", "", resp.content.strip()).strip().rstrip("`")
                decision = json.loads(content)
            except Exception as exc:
                logger.warning("[Drive] 探索推理失败 step=%d: %s", step, exc)
                break

            tool   = decision.get("tool", "")
            reason = decision.get("reason", "")

            if decision.get("done") or tool == "stop":
                logger.info("[Drive] EXPLORING 自主结束 step=%d：%s", step, reason)
                episode_memory.add(EventType.ACTION_TAKEN, f"探索结束：{reason}")
                break

            logger.info("[Drive] EXPLORING step=%d  tool=%s  reason=%s", step, tool, reason)
            result = await self._exec_explore_tool(tool, decision.get("params", {}))
            logger.info("[Drive] EXPLORING step=%d  result=%s", step, result)

            history.append({"step": step + 1, "tool": tool, "reason": reason, "result": result})
            episode_memory.add(
                EventType.ACTION_TAKEN,
                f"探索步骤{step + 1}[{tool}]：{reason} → {result}",
            )
            self._last_action_ts = time.time()

        logger.info("[Drive] 探索会话结束，共 %d 步", len(history))

    async def _exec_explore_tool(self, tool: str, params: dict) -> str:
        """执行探索中 LLM 决策的工具调用，导航工具可被打断。"""
        try:
            if tool == "explore_direction":
                return await self._exec_explore_direction(params.get("direction", "forward"))
            elif tool == "go2_sport":
                return await go2_sport(params.get("cmd", ""))
            elif tool == "go2_observe":
                return await go2_observe(params.get("question", "描述当前场景"))
            elif tool == "navigator_go":
                from cloud.go2.navigation.navigator import navigator
                name = params.get("name", "")
                nav_task = asyncio.create_task(navigator.go_to(name))
                while not nav_task.done():
                    if self.user_interrupt or self._person_present:
                        nav_task.cancel()
                        navigator.stop()
                        return "导航被打断"
                    await asyncio.sleep(0.5)
                return nav_task.result()
            elif tool == "tag_location":
                from cloud.go2.agentcore.memory import spatial as spatial_memory
                odom = go2.odom
                if not odom:
                    return "无法标记：odom 数据不可用"
                return spatial_memory.tag_location(params.get("name", "unknown"), odom)
            else:
                return f"未知工具: {tool}"
        except Exception as exc:
            return f"执行失败: {exc}"

    async def _exec_explore_direction(self, direction: str) -> str:
        """explore_direction 工具的实现：用地图算出安全目标坐标，Navigator 闭环导过去。

        地图不可用时降级为定时前进（2s）。
        """
        from cloud.go2.agentcore.memory import spatial as spatial_memory
        from cloud.go2.navigation.navigator import navigator

        odom = go2.odom
        if not odom:
            return "无法移动：odom 不可用"

        grid_obj = go2.occupancy_grid
        target   = frontier_mod.find_exploration_target(grid_obj, odom, direction) if grid_obj else None

        if target is None:
            # 地图不可用或该方向被堵死，降级：用 move_velocity 前进 2s
            logger.info("[Drive] explore_direction 无地图/方向受阻，降级前进 2s")
            import asyncio as _aio
            go2.move_velocity(0.3, 0.0, 0.0)
            await _aio.sleep(2.0)
            go2.move_velocity(0.0, 0.0, 0.0)
            new_odom = go2.odom
            pos = f"({new_odom['x']:.2f}, {new_odom['y']:.2f})" if new_odom else "未知"
            nav_summary = f"地图不可用，短距前进完成，当前位置 {pos}"
            observation = await go2_observe("描述当前看到的场景，有什么值得继续探索的")
            return f"{nav_summary} | 到达后观察：{observation}"

        target_x, target_y = target
        tmp_name = f"_explore_{int(time.time())}"
        spatial_memory.tag_location(tmp_name, {"x": target_x, "y": target_y, "heading": 0.0})

        nav_task = asyncio.create_task(navigator.go_to(tmp_name))
        try:
            while not nav_task.done():
                if self.user_interrupt or self._person_present:
                    nav_task.cancel()
                    navigator.stop()
                    return "导航被打断"
                await asyncio.sleep(0.5)
            result = nav_task.result()
        finally:
            spatial_memory.delete_location(tmp_name)

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
        return f"{nav_summary} | 到达后观察：{observation}"

    async def _do_social(self) -> None:
        """SOCIAL 状态：观察画面中的人，LLM 决策是否执行互动动作。"""
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
        """启动内驱动循环，重置所有状态，创建后台任务。"""
        if self._task is not None:
            self._task.cancel()
        self._state          = MotivationalState.IDLE
        self._curiosity      = 0
        self._person_present = False
        self._task           = asyncio.create_task(self._run_loop())
        logger.info("[Drive] 内驱动循环已启动")

    def stop(self) -> None:
        """停止内驱动循环，取消后台任务。"""
        if self._task is not None:
            self._task.cancel()
            self._task = None
        logger.info("[Drive] 内驱动循环已停止")


drive = Drive()
