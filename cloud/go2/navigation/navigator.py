"""
导航控制器，负责将机器人从当前位置引导到目标地点。
只在新鲜同帧的已知自由空间规划，所有退出路径停止。
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

NAV_TIMEOUT          = 60.0   # s
POSE_STALE_S         = 1.0    # s
MAP_STALE_S          = 2.0    # s
ARRIVAL_THRESHOLD    = 0.3    # m
HEADING_THRESHOLD    = 0.25   # rad，对齐朝向误差上限
WALK_SPEED           = 0.3    # m/s
TURN_SPEED           = 0.5    # rad/s
KP_YAW               = 0.8
STUCK_CHECK_INTERVAL = 6.0    # s
STUCK_DISTANCE       = 0.30   # m，卡住判定
MAX_RETRIES          = 6
MAX_REPLANS          = 20
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
    """Fail-closed local odom navigation with explicit task results."""

    def __init__(self) -> None:
        self.mode = NavMode.IDLE
        self.target: str | None = None
        self.patrol_stops: list[str] = []
        self._task: asyncio.Task | None = None
        self.task_id: str | None = None
        self.status = "idle"
        self.reason: str | None = None
        self.message: str | None = None
        self.current_path: list[tuple[int, int]] | None = None
        self.current_grid_obj = None
        self.actual_goal: dict | None = None
        self.owner_session_id: str | None = None
        self.last_trusted_pose: dict | None = None
        self.task_records: dict[str, dict] = {}

    @property
    def state(self) -> dict:
        return {"mode": self.mode, "task_id": self.task_id, "status": self.status,
                "reason": self.reason, "message": self.message, "target": self.target,
                "actual_goal": self.actual_goal, "patrol_stops": self.patrol_stops,
                "last_trusted_pose": self.last_trusted_pose,
                "owner_session_id": self.owner_session_id}

    def _begin(self, name: str, owner_session_id: str, task: asyncio.Task) -> str:
        import uuid
        self.stop()
        self.task_id = uuid.uuid4().hex
        self.status = "accepted"
        self.reason = None
        self.message = None
        self.last_trusted_pose = None
        self.actual_goal = None
        self.current_path = None
        self.current_grid_obj = None
        self.target = name
        self.mode = NavMode.GOING_TO
        self.owner_session_id = owner_session_id
        self._task = task
        self.task_records[self.task_id] = {"task_id": self.task_id, "status": "accepted",
                                           "target": name, "reason": None, "message": None}
        if len(self.task_records) > 50:
            self.task_records.pop(next(iter(self.task_records)))
        return self.task_id

    def task_result(self, task_id: str) -> dict | None:
        result = self.task_records.get(task_id)
        return result.copy() if result is not None else None

    def start_go_to(self, name: str, owner_session_id: str = "system") -> str:
        task = asyncio.create_task(self._run_to(name))
        return self._begin(name, owner_session_id, task)

    async def go_to(self, name: str, owner_session_id: str = "system") -> dict:
        self._begin(name, owner_session_id, asyncio.current_task())
        return await self._run_to(name)

    async def go_to_coordinate(self, x: float, y: float, owner_session_id: str = "system") -> dict:
        task = asyncio.current_task()
        self._begin("exploration target", owner_session_id, task)
        return await self._run_to("exploration target", {"name": "exploration target", "x": x, "y": y,
                                                          "heading": None, "frame_id": "odom",
                                                          "session_id": getattr(go2, "_odom_session_id", None)})

    async def _run_to(self, name: str, loc: dict | None = None) -> dict:
        task = asyncio.current_task()
        task_id = self.task_id
        try:
            if loc is None:
                loc = await spatial_memory.find_location(name)
            if loc is None:
                return self._finish("blocked", "location_not_found", f"找不到地点「{name}」", task_id)
            if loc.get("legacy_unanchored") or not loc.get("session_id"):
                return self._finish("blocked", "legacy_unanchored", f"地点「{name}」需在当前会话重新标定", task_id)
            if loc.get("session_id") != getattr(go2, "_odom_session_id", None):
                return self._finish("blocked", "map_mismatch", "地点不属于当前 odom 会话", task_id)
            if (loc.get("frame_id") != "odom" or
                    not all(isinstance(loc.get(k), (int, float)) and math.isfinite(loc[k]) for k in ("x", "y")) or
                    (loc.get("heading") is not None and not math.isfinite(loc["heading"]))):
                return self._finish("blocked", "invalid_location", "地点坐标无效", task_id)
            self.status = "executing"
            if task_id in self.task_records:
                self.task_records[task_id]["status"] = "executing"
            deadline = time.monotonic() + NAV_TIMEOUT
            last_replan = 0.0
            last_stuck = time.monotonic()
            last_pos = None
            replan_count = 0
            path = None
            grid_obj = None
            goal = None
            while time.monotonic() < deadline:
                snapshot, reason = self._snapshot()
                if reason:
                    return self._finish("failed", reason, "定位或地图不可用，已停止导航", task_id)
                odom, fresh_grid = snapshot
                self.last_trusted_pose = odom.copy()
                now = time.monotonic()
                if last_pos is None:
                    last_pos = (odom["x"], odom["y"])
                if path is None or now - last_replan >= REPLAN_INTERVAL:
                    planned = self._plan_path(loc, fresh_grid, odom)
                    if planned is None:
                        return self._finish("blocked", "no_known_free_path", "目标不在已知可通行区域", task_id)
                    path, grid_obj, goal = planned
                    self.current_path = path
                    self.current_grid_obj = grid_obj
                    self.actual_goal = {"x": goal[0], "y": goal[1], "adjusted": goal[2]}
                    last_replan = now
                    replan_count += 1
                    if replan_count > MAX_REPLANS:
                        return self._finish("blocked", "replan_budget_exceeded", "重规划次数过多", task_id)
                dx, dy = goal[0] - odom["x"], goal[1] - odom["y"]
                dist = math.hypot(dx, dy)
                if dist < ARRIVAL_THRESHOLD:
                    if loc.get("heading") is not None:
                        heading_error = _normalize_angle(loc["heading"] - odom["heading"])
                        if abs(heading_error) > HEADING_THRESHOLD:
                            go2.move_velocity(0, 0, max(-TURN_SPEED, min(TURN_SPEED, KP_YAW * heading_error)), deadman_s=0.3)
                            await asyncio.sleep(0.1)
                            continue
                    if await self._confirm_stopped(goal, loc.get("heading")):
                        suffix = "停靠点" if goal[2] else f"「{name}」"
                        return self._finish("succeeded", None, f"已到达{suffix}", task_id)
                    return self._finish("failed", "arrival_unconfirmed", "未能确认停稳抵达", task_id)
                if now - last_stuck >= STUCK_CHECK_INTERVAL:
                    if math.hypot(odom["x"] - last_pos[0], odom["y"] - last_pos[1]) < STUCK_DISTANCE:
                        go2.move_velocity(0, 0, 0)
                        return self._finish("blocked", "stuck", "移动受阻，已停止", task_id)
                    last_pos = (odom["x"], odom["y"])
                    last_stuck = now
                if _distance_to_path(path, grid_obj, odom["x"], odom["y"]) > MAX_PATH_DEVIATION:
                    go2.move_velocity(0, 0, 0)
                    path = None
                    continue
                lx, ly = _find_lookahead_on_path(path, grid_obj, odom["x"], odom["y"], odom["heading"],
                                                  {"x": goal[0], "y": goal[1]})
                heading_error = _normalize_angle(math.atan2(ly - odom["y"], lx - odom["x"]) - odom["heading"])
                if abs(heading_error) > PATH_HEADING_INHIBIT:
                    vx, yaw = 0.0, math.copysign(TURN_SPEED, heading_error)
                else:
                    vx, yaw = min(WALK_SPEED, max(MIN_LINEAR_VEL, dist * 0.8)), max(-TURN_SPEED, min(TURN_SPEED, KP_YAW * heading_error))
                go2.move_velocity(vx, 0, yaw, deadman_s=0.3)
                await asyncio.sleep(0.1)
            return self._finish("failed", "navigation_timeout", "导航超时，已停止", task_id)
        except asyncio.CancelledError:
            return self._finish("cancelled", "cancelled", "导航已取消", task_id)
        except (RuntimeError, ValueError) as exc:
            logger.warning("[Go2/Nav] navigation failed: %s", exc)
            return self._finish("failed", "control_error", "控制连接异常，已停止", task_id)
        finally:
            try:
                go2.move_velocity(0, 0, 0)
            except (RuntimeError, ValueError):
                logger.warning("[Go2/Nav] stop packet could not be sent")
            if self._task is task:
                self._task = None
                self.mode = NavMode.IDLE
                self.owner_session_id = None
                self.current_path = None
                self.current_grid_obj = None

    def _snapshot(self):
        now = time.monotonic()
        if not getattr(go2, "is_connected", False):
            return None, "disconnected"
        pose_age = getattr(go2, "odom_received_at", None)
        map_age = getattr(go2, "voxel_received_at", None)
        if pose_age is None or now - pose_age > POSE_STALE_S:
            return None, "localization_unavailable"
        if map_age is None or now - map_age > MAP_STALE_S:
            return None, "map_unavailable"
        pose_frame = getattr(go2, "odom_frame_id", None)
        map_frame = getattr(go2, "voxel_frame_id", None)
        if not pose_frame or not map_frame or pose_frame != map_frame or pose_frame != "odom":
            return None, "frame_mismatch"
        odom = go2.odom
        if not odom or not all(k in odom for k in ("x", "y", "heading")):
            return None, "localization_unavailable"
        try:
            grid = go2.occupancy_grid
        except (ValueError, TypeError):
            return None, "map_unavailable"
        if grid is None or grid.frame_id != map_frame or not getattr(grid, "has_free_space_evidence", False):
            return None, "map_unavailable"
        return (odom, grid), None

    def _plan_path(self, loc: dict, grid_obj, odom: dict):
        try:
            start = grid_obj.odom_to_grid(odom["x"], odom["y"])
            raw_goal = grid_obj.odom_to_grid(loc["x"], loc["y"])
        except ValueError:
            return None
        if not grid_obj.is_free(*start):
            return None
        goal = raw_goal
        if not grid_obj.is_free(*goal):
            # Only known free cells reachable from the robot may be stop points.
            candidates = []
            radius = math.ceil(ARRIVAL_THRESHOLD / grid_obj.resolution)
            for dy in range(-radius, radius + 1):
                for dx in range(-radius, radius + 1):
                    candidate = (raw_goal[0] + dx, raw_goal[1] + dy)
                    if not grid_obj.is_free(*candidate):
                        continue
                    wx, wy = grid_obj.grid_to_odom(*candidate)
                    if math.hypot(wx - loc["x"], wy - loc["y"]) <= ARRIVAL_THRESHOLD:
                        candidates.append((math.hypot(wx - loc["x"], wy - loc["y"]), candidate))
            for _, candidate in sorted(candidates):
                path = astar(grid_obj.grid, start, candidate)
                if path:
                    goal = candidate
                    break
            else:
                return None
        path = astar(grid_obj.grid, start, goal)
        if path is None:
            return None
        wx, wy = grid_obj.grid_to_odom(*goal)
        return path, grid_obj, (wx, wy, goal != raw_goal)

    async def _confirm_stopped(self, goal, heading):
        go2.move_velocity(0, 0, 0)
        previous_stamp = getattr(go2, "odom_received_at", None)
        previous_pose = go2.odom
        samples = 0
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline and samples < 3:
            await asyncio.sleep(0.1)
            snapshot, reason = self._snapshot()
            if reason:
                return False
            pose, _ = snapshot
            stamp = getattr(go2, "odom_received_at", None)
            if stamp == previous_stamp:
                continue
            if math.hypot(pose["x"] - goal[0], pose["y"] - goal[1]) >= ARRIVAL_THRESHOLD:
                return False
            if heading is not None and abs(_normalize_angle(pose["heading"] - heading)) > HEADING_THRESHOLD:
                return False
            if math.hypot(pose["x"] - previous_pose["x"], pose["y"] - previous_pose["y"]) > 0.02:
                return False
            previous_stamp, previous_pose = stamp, pose
            samples += 1
        return samples == 3

    def _finish(self, status: str, reason: str | None, message: str, task_id: str | None) -> dict:
        result = {"task_id": task_id, "status": status, "reason": reason, "message": message,
                  "last_trusted_pose": self.last_trusted_pose, "actual_goal": self.actual_goal}
        if self.task_id == task_id:
            self.status, self.reason, self.message = status, reason, message
        if task_id in self.task_records:
            self.task_records[task_id] = result.copy()
        return result

    async def start_patrol(self, stops: list[str], owner_session_id: str = "system") -> dict:
        # Patrol needs a persistent map. The current rolling point cloud supplies
        # obstacles only, so do not start an autonomous movement loop.
        self.status, self.reason, self.message = "blocked", "map_unavailable", "巡逻需已验证的持久地图"
        return {"status": self.status, "reason": self.reason, "message": self.message}

    def stop(self, owner_session_id: str | None = None) -> bool:
        if owner_session_id is not None and self.owner_session_id != owner_session_id:
            return False
        if self._task and not self._task.done():
            self._task.cancel()
            self.status, self.reason, self.message = "cancelled", "cancelled", "导航已取消"
            if self.task_id in self.task_records:
                self.task_records[self.task_id].update(status="cancelled", reason="cancelled", message="导航已取消")
        self.mode = NavMode.IDLE
        self.target = None
        self.patrol_stops = []
        self.owner_session_id = None
        try:
            go2.move_velocity(0, 0, 0)
        except (RuntimeError, ValueError):
            pass
        return True


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
