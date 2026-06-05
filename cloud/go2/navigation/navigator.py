import asyncio
import logging
import math
import random
import time
from enum import Enum
from typing import Optional

from cloud.go2.connection import go2
from cloud.go2.agentcore.memory import spatial as spatial_memory
from cloud.go2.navigation.astar import astar

ARRIVAL_THRESHOLD  = 0.3
HEADING_THRESHOLD  = 0.25
WALK_SPEED         = 0.3
TURN_SPEED         = 0.5
KP_YAW             = 0.8
STUCK_CHECK_INTERVAL = 2.0
STUCK_DISTANCE     = 0.05
STUCK_BACKUP_TIME  = 2.5
MAX_RETRIES        = 6
TRAJ_INTERVAL      = 5.0
MAX_SPIN_TIME      = 10.0


class NavMode(str, Enum):
    IDLE       = "idle"
    GOING_TO   = "going_to"
    PATROLLING = "patrolling"


class Navigator:
    def __init__(self) -> None:
        self.mode: NavMode        = NavMode.IDLE
        self.target: Optional[str] = None
        self.patrol_stops: list[str] = []
        self._task: Optional[asyncio.Task] = None

    @property
    def state(self) -> dict:
        return {
            "mode":         self.mode,
            "target":       self.target,
            "patrol_stops": self.patrol_stops,
            "odom":         go2.odom,
        }

    async def go_to(self, name: str) -> str:
        loc = await spatial_memory.find_location(name)
        if loc is None:
            return f"找不到地点「{name}」，请先用 tag_location 保存"
        self.mode   = NavMode.GOING_TO
        self.target = name
        self._task  = asyncio.current_task()
        try:
            await go2.set_obstacle_avoidance(True)
        except Exception:
            pass
        try:
            deadline = time.monotonic() + 10.0
            while go2.occupancy_grid is None and time.monotonic() < deadline:
                await asyncio.sleep(0.5)
            grid_obj = go2.occupancy_grid
            if grid_obj is not None:
                path = self._plan_path(loc)
                logging.info("[Nav] A* 路径点数: %s", len(path) if path else None)
                if path and len(path) > 1:
                    return await self._follow_path(path, grid_obj, loc)
            else:
                logging.warning("[Nav] 体素地图 10 秒内未就绪，降级直线导航")
            return await self._navigate_to(loc)
        finally:
            self.mode   = NavMode.IDLE
            self.target = None
            try:
                await go2.set_obstacle_avoidance(False)
            except Exception:
                pass

    async def start_patrol(self, stops: list[str]) -> None:
        self.stop()
        self.patrol_stops = stops
        self.mode = NavMode.PATROLLING
        self._task = asyncio.create_task(self._patrol_loop(stops))

    def stop(self) -> None:
        if self._task and not self._task.done():
            self._task.cancel()
        self.mode         = NavMode.IDLE
        self.target       = None
        self.patrol_stops = []
        try:
            go2.move_velocity(0, 0, 0)
        except RuntimeError:
            pass

    async def _navigate_to(self, loc: dict, retries: int = 0) -> str:
        last_stuck_check = time.monotonic()
        last_traj_tick   = time.monotonic()
        last_pos = {"x": 0.0, "y": 0.0}
        spin_start: Optional[float] = None
        odom = go2.odom
        if odom:
            last_pos = {"x": odom["x"], "y": odom["y"]}

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

            if dist < ARRIVAL_THRESHOLD:
                go2.move_velocity(0, 0, 0)
                return f"已到达「{loc['name']}」"

            target_heading = math.atan2(dy, dx)
            heading_error  = _normalize_angle(target_heading - odom["heading"])

            now = time.monotonic()

            if abs(heading_error) > HEADING_THRESHOLD:
                if spin_start is None:
                    spin_start = now
                elif now - spin_start > MAX_SPIN_TIME:
                    # 长时间无法对准朝向，视为被障碍物干扰，触发脱困
                    go2.move_velocity(0, 0, 0)
                    if retries >= MAX_RETRIES:
                        return f"无法到达「{loc['name']}」，已重试 {retries} 次"
                    await self._escape_obstacle()
                    return await self._navigate_to(loc, retries + 1)
                sign = 1.0 if heading_error > 0 else -1.0
                go2.move_velocity(0.0, 0.0, TURN_SPEED * sign)
            else:
                spin_start = None
                vx   = min(WALK_SPEED, dist * 0.8)
                vyaw = KP_YAW * heading_error
                go2.move_velocity(vx, 0.0, vyaw)

            if now - last_stuck_check >= STUCK_CHECK_INTERVAL:
                moved = math.sqrt(
                    (odom["x"] - last_pos["x"]) ** 2 +
                    (odom["y"] - last_pos["y"]) ** 2
                )
                # 只有在前进阶段（朝向已对准）位移不足才算卡死，纯转向阶段不计入
                if moved < STUCK_DISTANCE and abs(heading_error) <= HEADING_THRESHOLD:
                    go2.move_velocity(0, 0, 0)
                    if retries >= MAX_RETRIES:
                        return f"无法到达「{loc['name']}」，已重试 {retries} 次"
                    await self._escape_obstacle()
                    return await self._navigate_to(loc, retries + 1)
                last_pos         = {"x": odom["x"], "y": odom["y"]}
                last_stuck_check = now

            if now - last_traj_tick >= TRAJ_INTERVAL:
                spatial_memory.record_trajectory_tick(odom)
                last_traj_tick = now

            await asyncio.sleep(0.1)

    def _plan_path(self, goal_loc: dict) -> list[tuple[int, int]] | None:
        grid_obj = go2.occupancy_grid
        if grid_obj is None:
            return None
        odom = go2.odom
        if not odom:
            return None
        start = grid_obj.odom_to_grid(odom["x"], odom["y"])
        goal  = grid_obj.odom_to_grid(goal_loc["x"], goal_loc["y"])
        if not grid_obj.is_free(*start):
            return None
        return astar(grid_obj.grid, start, goal)

    async def _follow_path(self, path: list[tuple[int, int]], grid_obj, goal_loc: dict) -> str:
        WAYPOINT_RADIUS = 0.3
        REPLAN_INTERVAL = 3.0

        last_replan = time.monotonic()
        i = 1  # skip start node

        while i < len(path):
            wx, wy = grid_obj.grid_to_odom(*path[i])
            waypoint = {"name": goal_loc["name"], "x": wx, "y": wy}

            result = await self._navigate_to(waypoint, retries=0)
            if "无法到达" in result:
                return result

            i += 1

            if time.monotonic() - last_replan >= REPLAN_INTERVAL:
                new_path = self._plan_path(goal_loc)
                if new_path and len(new_path) > 1:
                    path = new_path
                    grid_obj = go2.occupancy_grid or grid_obj
                    i = 1
                last_replan = time.monotonic()

        return await self._navigate_to(goal_loc)

    async def _escape_obstacle(self) -> None:
        # 后退
        go2.move_velocity(-WALK_SPEED, 0.0, 0.0)
        await asyncio.sleep(STUCK_BACKUP_TIME)
        go2.move_velocity(0, 0, 0)
        # 随机左转或右转，角度在 60°-120° 之间
        turn_sign = 1.0 if random.random() > 0.5 else -1.0
        turn_time = random.uniform(0.8, 1.4)
        go2.move_velocity(0.0, 0.0, TURN_SPEED * turn_sign)
        await asyncio.sleep(turn_time)
        go2.move_velocity(0, 0, 0)
        # 稍微前进一步，离开障碍物附近
        go2.move_velocity(WALK_SPEED, 0.0, 0.0)
        await asyncio.sleep(0.6)
        go2.move_velocity(0, 0, 0)

    async def _patrol_loop(self, stops: list[str]) -> None:
        try:
            await go2.set_obstacle_avoidance(True)
        except Exception:
            pass
        try:
            while True:
                for name in stops:
                    self.target = name
                    loc = await spatial_memory.find_location(name)
                    if loc is not None:
                        await self._navigate_to(loc)
                    await asyncio.sleep(2.0)
        finally:
            try:
                await go2.set_obstacle_avoidance(False)
            except Exception:
                pass


def _normalize_angle(angle: float) -> float:
    while angle > math.pi:
        angle -= 2.0 * math.pi
    while angle < -math.pi:
        angle += 2.0 * math.pi
    return angle


navigator = Navigator()
