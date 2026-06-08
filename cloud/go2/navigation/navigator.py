"""
导航控制器，负责将机器人从当前位置引导到目标地点。
有体素地图时走 A* 路径规划 + 前瞻跟随，无地图时降级直线导航。
支持单次 go_to、多点巡逻 patrol，以及卡住逃脱和定期重规划。
"""
import asyncio
import logging
import math
import time
from enum import Enum
from typing import Optional

from cloud.go2.connection import go2
from cloud.go2.agentcore.memory import spatial as spatial_memory
from cloud.go2.navigation.astar import astar

logger = logging.getLogger(__name__)

ARRIVAL_THRESHOLD    = 0.3    # m
HEADING_THRESHOLD    = 0.25   # rad，对齐朝向误差上限
WALK_SPEED           = 0.3    # m/s
TURN_SPEED           = 0.5    # rad/s
KP_YAW               = 0.8
STUCK_CHECK_INTERVAL = 6.0    # s
STUCK_DISTANCE       = 0.30   # m，卡住判定
MAX_RETRIES          = 6
TRAJ_INTERVAL        = 5.0    # s
MIN_LINEAR_VEL       = 0.1    # m/s，路径跟随最低线速度
LOOKAHEAD_DIST       = 0.6    # m，前瞻距离
REPLAN_INTERVAL      = 1.0    # s，重规划间隔
MAX_PATH_DEVIATION   = 0.8    # m，偏离路径超过此值立即重规划
MAX_SPIN_TIME        = 10.0   # s，初始转向超时
# 偏角超过此值时纯转向，否则边走边修正
PATH_HEADING_INHIBIT = 0.5    # rad (~28°)


class NavMode(str, Enum):
    IDLE       = "idle"
    GOING_TO   = "going_to"
    PATROLLING = "patrolling"


class Navigator:
    """核心导航类，管理导航任务的生命周期（go_to / patrol / stop）。"""

    def __init__(self) -> None:
        self.mode: NavMode         = NavMode.IDLE
        self.target: Optional[str] = None
        self.patrol_stops: list[str] = []
        self._task: Optional[asyncio.Task] = None
        self.current_path: list[tuple[int, int]] | None = None
        self.current_grid_obj = None

    @property
    def state(self) -> dict:
        return {
            "mode":         self.mode,
            "target":       self.target,
            "patrol_stops": self.patrol_stops,
            "odom":         go2.odom,
        }

    # ── 公开接口 ──────────────────────────────────────────────────────────

    async def go_to(self, name: str) -> str:
        """导航到空间记忆中的指定地点，有地图走 A*，否则降级直线。"""
        loc = await spatial_memory.find_location(name)
        if loc is None:
            logger.warning("[Go2/Nav] 找不到地点「%s」", name)
            return f"找不到地点「{name}」，请先用 tag_location 保存"
        self.mode   = NavMode.GOING_TO
        self.target = name
        self._task  = asyncio.current_task()
        odom = go2.odom
        logger.info(
            "[Go2/Nav] 开始导航 → 「%s」(%.2f, %.2f)  当前位置=(%.2f, %.2f)",
            name, loc["x"], loc["y"],
            odom.get("x", 0.0) if odom else 0.0,
            odom.get("y", 0.0) if odom else 0.0,
        )
        try:
            # 等待体素地图就绪（最多 10 秒），有图则走 A*，否则直线导航
            deadline = time.monotonic() + 10.0
            while go2.occupancy_grid is None and time.monotonic() < deadline:
                await asyncio.sleep(0.5)
            if go2.occupancy_grid is not None:
                path = self._plan_path(loc)
                logger.info("[Go2/Nav] A* 路径点数: %s", len(path) if path else "无路径")
                if path and len(path) > 1:
                    # 用规划时最新的地图，确保路径格索引与坐标系一致
                    grid_obj = go2.occupancy_grid
                    return await self._follow_path(path, grid_obj, loc)
            else:
                logger.warning("[Go2/Nav] 体素地图 10 秒内未就绪，降级直线导航")
            return await self._navigate_to(loc)
        finally:
            self.mode             = NavMode.IDLE
            self.target           = None
            self.current_path     = None
            self.current_grid_obj = None

    async def start_patrol(self, stops: list[str]) -> None:
        """启动巡逻任务，按顺序循环访问多个地点。"""
        self.stop()
        self.patrol_stops = stops
        self.mode = NavMode.PATROLLING
        self._task = asyncio.create_task(self._patrol_loop(stops))

    def stop(self) -> None:
        """立即取消当前导航/巡逻任务并停止机器人。"""
        if self._task and not self._task.done():
            self._task.cancel()
        self.mode         = NavMode.IDLE
        self.target       = None
        self.patrol_stops = []
        try:
            go2.move_velocity(0, 0, 0)
        except RuntimeError:
            pass

    # ── 直线导航（无地图降级）────────────────────────────────────────────

    async def _navigate_to(self, loc: dict, retries: int = 0) -> str:
        """直线导航：阶段1初始转向，阶段2前进+比例偏航修正，卡住则逃脱重试。"""
        odom_init = go2.odom
        last_pos  = {"x": odom_init["x"], "y": odom_init["y"]} if odom_init else {"x": 0.0, "y": 0.0}
        last_stuck_check = time.monotonic()
        last_traj_tick   = time.monotonic()
        spin_start: Optional[float] = None
        phase = "initial_rotation"

        while True:
            if self.mode == NavMode.IDLE:
                go2.move_velocity(0, 0, 0)
                return "导航已取消"

            odom = go2.odom
            if not odom:
                await asyncio.sleep(0.1)
                continue

            dx   = loc["x"] - odom["x"]
            dy   = loc["y"] - odom["y"]
            dist = math.sqrt(dx * dx + dy * dy)
            now  = time.monotonic()

            if dist < ARRIVAL_THRESHOLD:
                go2.move_velocity(0, 0, 0)
                if loc.get("heading") is not None:
                    await self._align_heading(loc["heading"])
                logger.info("[Go2/Nav] 到达「%s」pos=(%.2f,%.2f)", loc["name"], odom["x"], odom["y"])
                return f"已到达「{loc['name']}」"

            target_heading = math.atan2(dy, dx)
            heading_error  = _normalize_angle(target_heading - odom["heading"])

            # 阶段一：初始转向
            if phase == "initial_rotation":
                if abs(heading_error) <= HEADING_THRESHOLD:
                    phase = "path_following"
                    spin_start = None
                    last_pos = {"x": odom["x"], "y": odom["y"]}
                    last_stuck_check = now
                    logger.info("[Go2/Nav] 初始转向完成，开始前进 dist=%.2fm", dist)
                else:
                    if spin_start is None:
                        spin_start = now
                    elif now - spin_start > MAX_SPIN_TIME:
                        go2.move_velocity(0, 0, 0)
                        if retries >= MAX_RETRIES:
                            return f"无法到达「{loc['name']}」，已重试 {retries} 次"
                        await self._escape_obstacle()
                        return await self._navigate_to(loc, retries + 1)
                    vyaw = KP_YAW * heading_error
                    vyaw = math.copysign(max(0.15, min(TURN_SPEED, abs(vyaw))), vyaw)
                    go2.move_velocity(0.0, 0.0, vyaw)

            # 阶段二：前进（含比例航向修正）
            if phase == "path_following":
                if abs(heading_error) > PATH_HEADING_INHIBIT:
                    sign = 1.0 if heading_error > 0 else -1.0
                    go2.move_velocity(0.0, 0.0, TURN_SPEED * sign)
                else:
                    vx   = max(MIN_LINEAR_VEL, min(WALK_SPEED, dist * 0.8))
                    vyaw = KP_YAW * heading_error
                    go2.move_velocity(vx, 0.0, vyaw)

                if now - last_stuck_check >= STUCK_CHECK_INTERVAL:
                    moved = math.sqrt(
                        (odom["x"] - last_pos["x"]) ** 2 +
                        (odom["y"] - last_pos["y"]) ** 2
                    )
                    if moved < STUCK_DISTANCE:
                        go2.move_velocity(0, 0, 0)
                        if retries >= MAX_RETRIES:
                            return f"无法到达「{loc['name']}」，已重试 {retries} 次"
                        await self._escape_obstacle()
                        new_grid = go2.occupancy_grid
                        if new_grid is not None:
                            new_path = self._plan_path(loc)
                            if new_path and len(new_path) > 1:
                                return await self._follow_path(new_path, new_grid, loc)
                        return await self._navigate_to(loc, retries + 1)
                    last_pos         = {"x": odom["x"], "y": odom["y"]}
                    last_stuck_check = now

            if now - last_traj_tick >= TRAJ_INTERVAL:
                spatial_memory.record_trajectory_tick(odom)
                last_traj_tick = now

            await asyncio.sleep(0.1)

    # ── A* 路径跟随 ───────────────────────────────────────────────────────

    def _plan_path(self, goal_loc: dict) -> list[tuple[int, int]] | None:
        """用当前体素地图和里程计调用 A*，返回格索引路径，无法规划时返回 None。"""
        grid_obj = go2.occupancy_grid
        if grid_obj is None:
            return None
        odom = go2.odom
        if not odom:
            return None
        start    = grid_obj.odom_to_grid(odom["x"], odom["y"])
        goal_raw = grid_obj.odom_to_grid(goal_loc["x"], goal_loc["y"])
        goal     = _nearest_free_cell(grid_obj, goal_raw, max_r=20)
        if goal is None:
            logger.warning("[Go2/Nav] A* 终点半径 1m 内无自由格 %s，降级直线", goal_raw)
            return None
        if goal != goal_raw:
            logger.debug("[Go2/Nav] A* 终点修正 %s → %s", goal_raw, goal)
        return astar(grid_obj.grid, start, goal)

    async def _follow_path(self, path: list[tuple[int, int]], grid_obj, goal_loc: dict) -> str:
        """按 A* 路径前瞻跟随，含偏离立即重规划、卡住逃脱、定期重规划。"""
        self.current_path     = path
        self.current_grid_obj = grid_obj
        last_replan      = time.monotonic()
        last_stuck_check = time.monotonic()
        last_traj_tick   = time.monotonic()
        retries          = 0
        odom_init        = go2.odom
        last_pos = {"x": odom_init["x"], "y": odom_init["y"]} if odom_init else {"x": 0.0, "y": 0.0}

        while True:
            if self.mode == NavMode.IDLE:
                go2.move_velocity(0, 0, 0)
                return "导航已取消"

            odom = go2.odom
            if not odom:
                await asyncio.sleep(0.1)
                continue

            # 剔除已经过的节点
            while len(path) > 1:
                wx, wy = grid_obj.grid_to_odom(*path[0])
                if math.sqrt((wx - odom["x"]) ** 2 + (wy - odom["y"]) ** 2) < ARRIVAL_THRESHOLD:
                    path = path[1:]
                else:
                    break

            now = time.monotonic()

            # 检查是否已到终点
            dx_goal = goal_loc["x"] - odom["x"]
            dy_goal = goal_loc["y"] - odom["y"]
            if math.sqrt(dx_goal ** 2 + dy_goal ** 2) < ARRIVAL_THRESHOLD:
                go2.move_velocity(0, 0, 0)
                if goal_loc.get("heading") is not None:
                    await self._align_heading(goal_loc["heading"])
                logger.info("[Go2/Nav] 到达「%s」pos=(%.2f,%.2f)", goal_loc["name"], odom["x"], odom["y"])
                return f"已到达「{goal_loc['name']}」"

            # 前瞻目标点
            lx, ly = _find_lookahead_on_path(path, grid_obj, odom["x"], odom["y"], odom["heading"], goal_loc)
            dx = lx - odom["x"]
            dy = ly - odom["y"]
            target_heading = math.atan2(dy, dx)
            heading_error  = _normalize_angle(target_heading - odom["heading"])

            if abs(heading_error) > PATH_HEADING_INHIBIT:
                sign = 1.0 if heading_error > 0 else -1.0
                go2.move_velocity(0.0, 0.0, TURN_SPEED * sign)
            else:
                dist_to_lookahead = math.sqrt(dx ** 2 + dy ** 2)
                vx   = max(MIN_LINEAR_VEL, min(WALK_SPEED, dist_to_lookahead * 0.8))
                vyaw = KP_YAW * heading_error
                go2.move_velocity(vx, 0.0, vyaw)

            # 偏离路径检测：超过阈值立即重规划，不等定时器
            deviation = _distance_to_path(path, grid_obj, odom["x"], odom["y"])
            if deviation > MAX_PATH_DEVIATION:
                logger.info("[Go2/Nav] 偏离路径 %.2fm，立即重规划", deviation)
                new_path = self._plan_path(goal_loc)
                if new_path and len(new_path) > 1:
                    path     = new_path
                    grid_obj = go2.occupancy_grid or grid_obj
                    self.current_path     = path
                    self.current_grid_obj = grid_obj
                last_replan = now

            # 卡住检测
            if now - last_stuck_check >= STUCK_CHECK_INTERVAL:
                moved = math.sqrt(
                    (odom["x"] - last_pos["x"]) ** 2 +
                    (odom["y"] - last_pos["y"]) ** 2
                )
                if moved < STUCK_DISTANCE:
                    go2.move_velocity(0, 0, 0)
                    retries += 1
                    if retries >= MAX_RETRIES:
                        return f"无法到达「{goal_loc['name']}」，已重试 {retries} 次"
                    await self._escape_obstacle()
                    new_path = self._plan_path(goal_loc)
                    if new_path and len(new_path) > 1:
                        path     = new_path
                        grid_obj = go2.occupancy_grid or grid_obj
                        self.current_path     = path
                        self.current_grid_obj = grid_obj
                    fresh = go2.odom
                    if fresh:
                        last_pos = {"x": fresh["x"], "y": fresh["y"]}
                    last_stuck_check = time.monotonic()
                    # 不重置 retries，累计超过 MAX_RETRIES 则放弃
                    continue
                last_pos         = {"x": odom["x"], "y": odom["y"]}
                last_stuck_check = now

            # 定期重规划
            if now - last_replan >= REPLAN_INTERVAL:
                new_path = self._plan_path(goal_loc)
                if new_path and len(new_path) > 1:
                    path     = new_path
                    grid_obj = go2.occupancy_grid or grid_obj
                    self.current_path     = path
                    self.current_grid_obj = grid_obj
                last_replan = now

            if now - last_traj_tick >= TRAJ_INTERVAL:
                spatial_memory.record_trajectory_tick(odom)
                last_traj_tick = now

            await asyncio.sleep(0.1)

    # ── 辅助方法 ──────────────────────────────────────────────────────────

    async def _align_heading(self, target_heading: float) -> None:
        """到达目标后，原地旋转对齐保存的朝向，超时则放弃。"""
        deadline = time.monotonic() + MAX_SPIN_TIME
        while time.monotonic() < deadline:
            odom = go2.odom
            if not odom:
                await asyncio.sleep(0.05)
                continue
            error = _normalize_angle(target_heading - odom["heading"])
            if abs(error) <= HEADING_THRESHOLD:
                go2.move_velocity(0, 0, 0)
                return
            vyaw = KP_YAW * error
            vyaw = math.copysign(max(0.15, min(TURN_SPEED, abs(vyaw))), vyaw)
            go2.move_velocity(0.0, 0.0, vyaw)
            await asyncio.sleep(0.05)
        go2.move_velocity(0, 0, 0)
        logger.warning("[Go2/Nav] _align_heading 超时，目标朝向=%.2f", target_heading)

    async def _escape_obstacle(self) -> None:
        """卡住时的逃脱序列：后退 → 随机转向 → 短暂前进。"""
        import random
        go2.move_velocity(-WALK_SPEED, 0.0, 0.0)
        await asyncio.sleep(1.5)
        go2.move_velocity(0, 0, 0)
        turn_sign = 1.0 if random.random() > 0.5 else -1.0
        go2.move_velocity(0.0, 0.0, TURN_SPEED * turn_sign)
        await asyncio.sleep(random.uniform(0.8, 1.2))
        go2.move_velocity(0, 0, 0)
        go2.move_velocity(WALK_SPEED, 0.0, 0.0)
        await asyncio.sleep(0.5)
        go2.move_velocity(0, 0, 0)

    async def _patrol_loop(self, stops: list[str]) -> None:
        """巡逻循环内部实现，无限循环访问各站点，每站停留 2s。"""
        while True:
            for name in stops:
                self.target = name
                loc = await spatial_memory.find_location(name)
                if loc is not None:
                    grid_obj = go2.occupancy_grid
                    if grid_obj is not None:
                        path = self._plan_path(loc)
                        if path and len(path) > 1:
                            grid_obj = go2.occupancy_grid
                            await self._follow_path(path, grid_obj, loc)
                            await asyncio.sleep(2.0)
                            continue
                    await self._navigate_to(loc)
                await asyncio.sleep(2.0)


# ── 模块级辅助函数 ────────────────────────────────────────────────────────

def _find_lookahead_on_path(
    path: list[tuple[int, int]],
    grid_obj,
    robot_x: float,
    robot_y: float,
    robot_heading: float,
    goal_loc: dict,
) -> tuple[float, float]:
    """在路径上找距机器人前方 LOOKAHEAD_DIST 处的目标点，无合适点则返回终点。"""
    for ix, iy in path:
        wx, wy = grid_obj.grid_to_odom(ix, iy)
        dx, dy = wx - robot_x, wy - robot_y
        dist = math.sqrt(dx * dx + dy * dy)
        if dist < LOOKAHEAD_DIST:
            continue
        # 只选前方点（与当前朝向点积 > 0，排除身后点）
        if math.cos(robot_heading) * dx + math.sin(robot_heading) * dy > 0:
            return wx, wy
    return goal_loc["x"], goal_loc["y"]


def _normalize_angle(angle: float) -> float:
    """将角度归一化到 [-π, π] 范围。"""
    while angle > math.pi:
        angle -= 2.0 * math.pi
    while angle < -math.pi:
        angle += 2.0 * math.pi
    return angle


def _distance_to_path(
    path: list[tuple[int, int]],
    grid_obj,
    robot_x: float,
    robot_y: float,
) -> float:
    """返回机器人到路径最近节点的距离（米），用于判断是否需要重规划。"""
    min_d2 = float("inf")
    for ix, iy in path:
        wx, wy = grid_obj.grid_to_odom(ix, iy)
        d2 = (wx - robot_x) ** 2 + (wy - robot_y) ** 2
        if d2 < min_d2:
            min_d2 = d2
    return math.sqrt(min_d2) if min_d2 < float("inf") else 0.0


def _nearest_free_cell(
    grid_obj, cell: tuple[int, int], max_r: int = 40
) -> tuple[int, int] | None:
    """BFS 找距 cell 最近的可通行格，目标点落在障碍内时使用，不会穿越障碍选到墙另一侧。"""
    from collections import deque
    if grid_obj.is_free(*cell):
        return cell
    queue: deque = deque([(cell, 0)])
    visited: set = {cell}
    dirs = [(1,0),(-1,0),(0,1),(0,-1),(1,1),(1,-1),(-1,1),(-1,-1)]
    while queue:
        (ix, iy), dist = queue.popleft()
        if dist >= max_r:
            continue
        for dx, dy in dirs:
            nb = (ix + dx, iy + dy)
            if nb in visited:
                continue
            visited.add(nb)
            if not (0 <= nb[0] < grid_obj.width[0] and 0 <= nb[1] < grid_obj.width[1]):
                continue
            if grid_obj.is_free(*nb):
                return nb
            queue.append((nb, dist + 1))
    return None


navigator = Navigator()
