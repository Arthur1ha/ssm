"""
机器人内驱动系统：空闲时按好奇心定期探索地图边界。
视觉与 LLM 处理互动和到达后的场景描述，不控制地图导航的启动或停止。
"""
import asyncio
import json
import logging
import re
import time
import uuid
from dataclasses import replace
from enum import Enum
from typing import Optional

from langchain_core.messages import SystemMessage, HumanMessage

from cloud.go2.connection import go2
from cloud.go2.agentcore.tools.tools import (
    go2_observe, get_text_llm, _VALID_SPORT_CMDS, _normalize_sport_action,
)
from cloud.go2.agentcore.tools.executor import execute_tool
from cloud.go2.execution import ExecutionContext, SYSTEM_CONTEXT
from cloud.go2.agentcore.perception.vision import VisionFrame
from cloud.go2.agentcore.soul import get_system_prompt
from cloud.go2.agentcore.personality.profile import get_profile
from cloud.go2.agentcore.personality.behavior import exploration_interval, social_action_candidates
from cloud.go2.agentcore.prompts import build_prompt
from cloud.go2.agentcore.memory.episode import episode_memory, EventType
from cloud.go2.space_context import prompt_space_context
from cloud.go2.navigation import frontier as frontier_mod
from cloud.peers.social import SocialState, activity_peers

logger = logging.getLogger(__name__)

_SOCIAL_CHECK_INTERVAL = 5    # 空闲期间有人脸信号时每 5 tick 评估互动


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
        targets = [{"unit_id": peer["unit_id"], "name": peer.get("name"),
                    "skills": (peer.get("skills") or [])[:5]} for peer in peers]
        prompt = (
            f"{profile['prompt']}\n"
            f"你的社交主动性：{score}/100。此值影响主动开口的倾向，"
            "但每条消息仍应与情境和同伴能力相关。\n"
            "你正在活动内评估是否主动向同伴说一句有价值的话。"
            "只根据当前活动和可靠发现判断；工作或睡觉等情境可保持安静。"
            "不能声称动作已完成或从普通视觉描述推断人物身份、偏好。"
            "只输出 JSON：{\"decision\":\"send_message\",\"target\":\"设备ID\","
            "\"content\":\"简短内容\",\"reason\":\"话题\"} 或"
            " {\"decision\":\"skip\",\"reason\":\"原因\"}。\n"
            f"当前活动：{json.dumps(context['activity'], ensure_ascii=False)}\n"
            f"机会：{json.dumps(opportunity, ensure_ascii=False)}\n"
            f"在线同伴：{json.dumps(targets, ensure_ascii=False)}\n"
            f"共享现状：{prompt_space_context()}"
        )
        try:
            response = await get_text_llm().ainvoke([HumanMessage(content=prompt)])
            content = re.sub(r"```(?:json)?\n?", "", response.content.strip()).strip().rstrip("`")
            decision = json.loads(content)
        except Exception as exc:
            logger.warning("[Drive/Social] evaluation failed: %s", exc)
            self._social_retry_after = time.time() + 30
            return
        if not isinstance(decision, dict) or decision.get("decision") not in {"send_message", "skip"}:
            logger.warning("[Drive/Social] invalid decision: %s", decision)
            self._social_retry_after = time.time() + 30
            return
        self._social_state.evaluated(opportunity)
        if decision.get("decision") != "send_message":
            logger.info("[Drive/Social] skip: %s", decision.get("reason", ""))
            return
        target = str(decision.get("target") or "")
        message = str(decision.get("content") or "").strip()[:1000]
        topic = str(decision.get("reason") or message)
        latest_activity, latest_peers = activity_peers(
            "go2", get_space_context("go2", state=_esp32_state)["peers"])
        if (not message or not latest_activity or latest_activity["activity_id"] != activity_id
                or target not in {peer["unit_id"] for peer in latest_peers}
                or not self._social_state.can_send(target, topic)
                or self._activity_id() != activity_id or self.user_interrupt):
            logger.info("[Drive/Social] send blocked by activity or budget")
            return
        from cloud.go2.agentcore.tools.tools import send_message
        outcome = send_message(target, message)
        if outcome.get("result") == "accepted":
            self._social_state.sent(target, topic)
            logger.info("[Drive/Social] sent target=%s content=%s message=%s conversation=%s",
                        target, message, outcome.get("message_id"), outcome.get("conversation_id"))
        else:
            logger.info("[Drive/Social] delivery rejected: %s", outcome)

    async def _execute_motion(self, name: str, params: dict) -> dict:
        if self.user_interrupt or not go2.is_connected:
            return {"result": "cancelled", "message": "自主模式已中断或设备已断开"}
        self._activity_event("action_progress", name, action=name, status="executing")
        result = await execute_tool(name, params, context=self._context)
        self._last_execution_result = result.get("result", "error")
        self._activity_event("action_result", self._result_text(result), action=name,
                             task_id=result.get("task_id"), status=self._last_execution_result)
        if go2.is_connected:
            asyncio.create_task(self._publish_motion_reply(name, params, result))
        return result

    async def _publish_motion_reply(self, name: str, params: dict, result: dict) -> None:
        from cloud.agent_runtime.loop import Run
        from cloud.agent_runtime.replies import persona_reply_prompt, valid_persona_reply, factual_fallback
        from cloud.api.intents import publish_agent_event

        if not go2.is_connected or self.user_interrupt:
            return
        run_id = "auto_" + uuid.uuid4().hex
        status = str(result.get("result") or "unknown")
        run = Run(run_id, "", "根据环境自主探索与互动", time.time() + 15,
                  steps=[{"step_id": run_id, "tool": name, "params": params,
                          "result": result}], status=status, agent_id="go2",
                  origin_kind="agent_autonomous")
        publish_agent_event("agent_progress", agent_id="go2", scope="space",
            space_id=self._context.space_id, source="autonomous", status=status,
            run_id=run_id, step_id=run_id, stage="observed")
        try:
            persona = get_profile()["prompt"]
            response = await get_text_llm().ainvoke([
                HumanMessage(content=persona_reply_prompt(persona, run))])
            answer = response.content
            if not valid_persona_reply(answer, run):
                raise ValueError("invalid_persona_reply")
        except Exception:
            answer = factual_fallback("go2", status, run)
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

    def on_identity_event(self, event) -> None:
        """身份仅作为语言/社交上下文，不直接切换状态或触发运动。"""
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
        while True:
            await asyncio.sleep(1)
            if self.user_interrupt or self._pending_question_id:
                continue

            await self._evaluate_social()

            self._curiosity += 1
            self._social_tick += 1

            # Exploration is scheduled by curiosity and the live map. Face
            # detection only schedules a separate social check between trips.
            profile = get_profile()
            threshold = exploration_interval(profile["traits"]["curiosity"])
            if self._curiosity >= threshold:
                logger.info("[Drive] explore candidate revision=%s curiosity=%s interval=%s",
                            profile["revision"], profile["traits"]["curiosity"], threshold)
                self._curiosity = 0
                self._state = MotivationalState.EXPLORING
                try:
                    await self._do_explore()
                finally:
                    self._state = MotivationalState.IDLE
            elif self._person_engaging and self._social_tick >= _SOCIAL_CHECK_INTERVAL:
                self._social_tick = 0
                await self._do_social()

    async def _do_explore(self) -> None:
        """Explore measured map frontiers; vision describes arrivals separately."""
        from cloud.go2.navigation.navigator import navigator

        if self.user_interrupt or self._pending_question_id:
            logger.info("[Drive] 探索进入即被打断")
            return

        visited: list[tuple[float, float]] = []
        rejected: list[tuple[float, float]] = []
        logger.info("[Drive] 开始地图驱动的自主探索")
        for step in range(15):
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
                    continue
                break
            visited.append(target)
            self._activity_event("action_result", self._result_text(result),
                                 action="explore_frontier", status="ok")
            # The camera reports what was reached; its availability never
            # changes the map target or authorizes movement.
            if time.time() >= self._vision_retry_after:
                try:
                    observation = await go2_observe("描述当前位置看到的场景，有什么值得记录的")
                    if observation and not observation.startswith("无可用视频帧"):
                        episode_memory.add(EventType.OBSERVATION, observation)
                        event = self._activity_event("observation", observation)
                        await self._evaluate_social(event)
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
                from cloud.go2.agentcore.memory import spatial as spatial_memory
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
                from cloud.go2.agentcore.tools.tools import send_message
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
        """SOCIAL 状态：观察画面中的人，LLM 决策是否执行互动动作。"""
        try:
            observation = await go2_observe("描述画面中的人在做什么")
        except Exception as exc:
            logger.warning("[Drive] observe 失败: %s", exc)
            return

        episode_memory.add(EventType.OBSERVATION, f"社交观察：{observation}")
        memory_ctx = episode_memory.format_context()
        profile = get_profile()
        boldness = profile["traits"]["boldness"]
        candidates = social_action_candidates(boldness, _VALID_SPORT_CMDS)
        prompt = (
            f"{self._identity_context()}\n"
            f"最新观察：{observation}\n"
            f"可选姿态动作：{', '.join(sorted(candidates))}；大胆程度 {boldness}/100。"
            "分数越低越倾向先观察或保持安静；不能改变运动安全条件。\n\n"
            f"决定是否对这个人做出互动（或什么都不做）。\n"
            f"直接输出 JSON，不含代码块：{{\"action\": \"动作名或null\", \"reason\": \"一句话\"}}"
        )
        try:
            bundle = build_prompt(
                "free_explore", personality=profile["prompt"], memory=memory_ctx,
                device_state=prompt + "\n当前空间现状：" + prompt_space_context(),
                memory_sources=("episode",), tool_version="go2-tools-v1",
            )
            self._last_prompt_metadata = bundle.metadata
            logger.info("[Go2/Prompt] %s", bundle.metadata)
            resp = await get_text_llm().ainvoke([
                SystemMessage(content=bundle.system),
                HumanMessage(content=bundle.human),
            ])
            content = re.sub(r"```(?:json)?\n?", "", resp.content.strip()).strip().rstrip("`")
            decision = json.loads(content)
        except Exception as exc:
            logger.warning("[Drive] 社交推理失败: %s", exc)
            return

        action = decision.get("action")
        reason = decision.get("reason", "")
        if action:
            canonical = _normalize_sport_action(str(action))
            if canonical is None or canonical not in candidates:
                logger.info("[Drive] SOCIAL 忽略无效姿态动作：%s", action)
                return
            try:
                result = await self._execute_motion("go2_sport", {"cmd": canonical})
                if result.get("result") != "ok":
                    return
                self._last_action_ts = time.time()
                episode_memory.add(EventType.ACTION_TAKEN, f"社交互动：执行了 {canonical}（{reason}）")
                logger.info("[Drive] SOCIAL 执行 %s：%s", canonical, reason)
            except Exception as exc:
                logger.warning("[Drive] 社交执行失败: %s", exc)
        else:
            logger.info("[Drive] SOCIAL 决定不互动：%s", reason)

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
