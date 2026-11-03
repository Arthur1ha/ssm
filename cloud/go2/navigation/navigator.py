"""
导航控制器，负责将机器人从当前位置引导到目标地点。
只在新鲜同帧的已知自由空间规划，所有退出路径停止。
"""
import asyncio
import logging
import math
import time
from enum import Enum
from typing import Callable, Optional

from cloud.go2.connection import go2
from cloud.agents.go2.memory import spatial as spatial_memory
from cloud.go2.navigation.astar import astar
from cloud.go2.control import control_lease, LeaseConflict
from cloud.observability import emit_agent_event

logger = logging.getLogger(__name__)

NAV_TIMEOUT          = 60.0   # s
POSE_STALE_S         = 1.0    # s
MAP_STALE_S          = 2.0    # s
TELEMETRY_RECOVERY_S = 3.0    # s; hold still while fresh odom/map packets resume
ARRIVAL_THRESHOLD    = 0.3    # m
HEADING_THRESHOLD    = 0.25   # rad，对齐朝向误差上限
WALK_SPEED           = 0.3    # m/s
TURN_SPEED           = 0.5    # rad/s
KP_YAW               = 0.8
MAX_RETRIES          = 6
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

    async def go_to_coordinate(self, x: float, y: float, owner_session_id: str = "system",
                               heading: float | None = None,
                               keepout: tuple[float, float, float] | None = None,
                               speed_limit: float | None = None,
                               holonomic: bool = False,
                               face_target: tuple[float, float] | None = None,
                               stop_when_near_target_m: float | None = None,
                               face_target_bias_rad: float | Callable[[], float] = 0.0) -> dict:
        task = asyncio.current_task()
        self._begin("exploration target", owner_session_id, task)
        return await self._run_to("exploration target", {"name": "exploration target", "x": x, "y": y,
                                                          "heading": heading, "frame_id": "odom",
                                                          "keepout": keepout,
                                                          "speed_limit": speed_limit,
                                                          "holonomic": holonomic,
                                                          "face_target": face_target,
                                                          "stop_when_near_target_m": stop_when_near_target_m,
                                                          "face_target_bias_rad": face_target_bias_rad,
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
            speed_limit = loc.get("speed_limit")
            if speed_limit is not None and (not isinstance(speed_limit, (int, float))
                                            or not math.isfinite(speed_limit)
                                            or not MIN_LINEAR_VEL <= speed_limit <= WALK_SPEED):
                return self._finish("blocked", "invalid_speed_limit", "导航速度限制无效", task_id)
            self.status = "executing"
            if task_id in self.task_records:
                self.task_records[task_id]["status"] = "executing"
            deadline = time.monotonic() + NAV_TIMEOUT
            last_replan = 0.0
            replan_count = 0
            path = None
            grid_obj = None
            goal = None
            sensor_gap_since = None
            last_command = None
            start_pose = None
            avoided_points = []
            while time.monotonic() < deadline:
                if self.owner_session_id != "system":
                    try:
                        control_lease.heartbeat_session(self.owner_session_id)
                    except LeaseConflict:
                        return self._finish("cancelled", "control_lease_lost", "运动租约已失效", task_id)
                snapshot, reason = self._snapshot()
                if reason:
                    now = time.monotonic()
                    recoverable = (reason in {"localization_unavailable", "map_unavailable"}
                                   and getattr(go2, "is_connected", False))
                    if sensor_gap_since is None or not recoverable:
                        try:
                            go2.move_velocity(0, 0, 0)
                        except (RuntimeError, ValueError):
                            pass
                    if recoverable:
                        if sensor_gap_since is None:
                            sensor_gap_since = now
                            logger.warning("[Go2/Nav] telemetry paused reason=%s diagnostics=%s",
                                           reason, getattr(go2, "sensor_diagnostics", None))
                        if now - sensor_gap_since < TELEMETRY_RECOVERY_S:
                            await asyncio.sleep(0.1)
                            continue
                    diagnostics = {
                        "sensor": getattr(go2, "sensor_diagnostics", None),
                        "last_command": last_command,
                        "gap_s": round(now - sensor_gap_since, 3) if sensor_gap_since is not None else None,
                        "displacement_m": round(math.hypot(
                            self.last_trusted_pose["x"] - start_pose["x"],
                            self.last_trusted_pose["y"] - start_pose["y"]), 3)
                            if self.last_trusted_pose and start_pose else None,
                    }
                    return self._finish("failed", reason, "定位或地图数据中断，已停止导航", task_id,
                                        diagnostics=diagnostics)
                if sensor_gap_since is not None:
                    logger.info("[Go2/Nav] telemetry resumed after %.2fs; replanning",
                                time.monotonic() - sensor_gap_since)
                    sensor_gap_since = None
                    path = None
                if loc["session_id"] != getattr(go2, "_odom_session_id", None):
                    return self._finish("blocked", "map_mismatch", "定位会话已变化，导航已停止", task_id)
                odom, fresh_grid = snapshot
                if start_pose is None:
                    start_pose = odom.copy()
                keepout = loc.get("keepout")
                if keepout and math.hypot(odom["x"] - keepout[0], odom["y"] - keepout[1]) < keepout[2]:
                    return self._finish("blocked", "target_keepout_breached", "目标安全距离不足", task_id)
                self.last_trusted_pose = odom.copy()
                near_radius = loc.get("stop_when_near_target_m")
                face_target = loc.get("face_target")
                if near_radius is not None and face_target is not None:
                    target_distance = math.hypot(
                        odom["x"] - face_target[0], odom["y"] - face_target[1])
                    if target_distance <= near_radius:
                        self.actual_goal = {"x": odom["x"], "y": odom["y"], "adjusted": True}
                        if await self._confirm_stopped((odom["x"], odom["y"], True), None):
                            return self._finish("succeeded", None, "已接近目标并停稳", task_id)
                        return self._finish("failed", "arrival_unconfirmed", "未能确认停稳抵达", task_id)
                now = time.monotonic()
                if path is None or now - last_replan >= REPLAN_INTERVAL:
                    planned = self._plan_path(loc, fresh_grid, odom, avoided_points)
                    if planned is None:
                        reason = "body_clearance_unavailable" if avoided_points else "no_known_free_path"
                        message = ("机身空间不足，地图中没有可绕行路线" if avoided_points
                                   else "目标不在已知可通行区域")
                        return self._finish("blocked", reason, message, task_id)
                    path, grid_obj, goal = planned
                    self.current_path = path
                    self.current_grid_obj = grid_obj
                    self.actual_goal = {"x": goal[0], "y": goal[1], "adjusted": goal[2]}
                    last_replan = now
                    replan_count += 1
                    emit_agent_event(
                        "navigation.progress", task_id=task_id,
                        replan_count=replan_count,
                        pose_odom_xy=[round(odom["x"], 3), round(odom["y"], 3)],
                        displacement_m=round(math.hypot(
                            odom["x"] - start_pose["x"],
                            odom["y"] - start_pose["y"]), 3),
                        goal_odom_xy=[round(goal[0], 3), round(goal[1], 3)],
                        last_command=last_command,
                    )
                dx, dy = goal[0] - odom["x"], goal[1] - odom["y"]
                dist = math.hypot(dx, dy)
                arrival_tolerance = ARRIVAL_THRESHOLD / 2 if near_radius is not None else ARRIVAL_THRESHOLD
                if dist < arrival_tolerance:
                    if loc.get("heading") is not None:
                        heading_error = _normalize_angle(loc["heading"] - odom["heading"])
                        if abs(heading_error) > HEADING_THRESHOLD:
                            turn_clear = getattr(fresh_grid, "is_turn_clear", None)
                            if turn_clear is None or not turn_clear(odom["x"], odom["y"]):
                                return self._finish("blocked", "turn_clearance_unavailable",
                                                    "原地转身空间不足，已停止", task_id)
                            go2.move_velocity(0, 0, max(-TURN_SPEED, min(TURN_SPEED, KP_YAW * heading_error)), deadman_s=0.3)
                            await asyncio.sleep(0.1)
                            continue
                    if await self._confirm_stopped(goal, loc.get("heading")):
                        suffix = "停靠点" if goal[2] else f"「{name}」"
                        return self._finish("succeeded", None, f"已到达{suffix}", task_id)
                    return self._finish("failed", "arrival_unconfirmed", "未能确认停稳抵达", task_id)
                if _distance_to_path(path, grid_obj, odom["x"], odom["y"]) > MAX_PATH_DEVIATION:
                    go2.move_velocity(0, 0, 0)
                    path = None
                    continue
                lx, ly = _find_lookahead_on_path(path, grid_obj, odom["x"], odom["y"], odom["heading"],
                                                  {"x": goal[0], "y": goal[1]},
                                                  front_only=not loc.get("holonomic", False))
                heading_error = _normalize_angle(math.atan2(ly - odom["y"], lx - odom["x"]) - odom["heading"])
                if loc.get("holonomic"):
                    speed = min(speed_limit or WALK_SPEED, max(MIN_LINEAR_VEL, dist * 0.8))
                    vx, vy = speed * math.cos(heading_error), speed * math.sin(heading_error)
                    face_target = loc.get("face_target")
                    if face_target is None:
                        yaw = 0.0
                    else:
                        bias = loc.get("face_target_bias_rad", 0.0)
                        if callable(bias):
                            bias = bias()
                        target_heading = math.atan2(
                            face_target[1] - odom["y"],
                            face_target[0] - odom["x"]) + bias
                        yaw_error = _normalize_angle(target_heading - odom["heading"])
                        yaw = max(-TURN_SPEED, min(TURN_SPEED, KP_YAW * yaw_error))
                elif abs(heading_error) > PATH_HEADING_INHIBIT:
                    vx, vy, yaw = 0.0, 0.0, math.copysign(TURN_SPEED, heading_error)
                else:
                    vx = min(speed_limit or WALK_SPEED, max(MIN_LINEAR_VEL, dist * 0.8))
                    vy = 0.0
                    yaw = max(-TURN_SPEED, min(TURN_SPEED, KP_YAW * heading_error))
                body_clear = getattr(fresh_grid, "is_body_clear", None)
                if body_clear is not None:
                    if not body_clear(odom["x"], odom["y"], odom["heading"]):
                        return self._finish("blocked", "body_clearance_unavailable",
                                            "当前机身朝向与障碍距离不足，已停止", task_id)
                    if vx == 0 and vy == 0 and yaw != 0:
                        turn_clear = getattr(fresh_grid, "is_turn_clear", None)
                        if turn_clear is not None and not turn_clear(odom["x"], odom["y"]):
                            return self._finish("blocked", "turn_clearance_unavailable",
                                                "原地转身空间不足，已停止", task_id)
                    c, s = math.cos(odom["heading"]), math.sin(odom["heading"])
                    predicted_x = odom["x"] + (vx * c - vy * s) * 0.3
                    predicted_y = odom["y"] + (vx * s + vy * c) * 0.3
                    if keepout and math.hypot(predicted_x - keepout[0], predicted_y - keepout[1]) < keepout[2]:
                        return self._finish("blocked", "target_keepout_breached", "下一步会进入目标安全区", task_id)
                    if not body_clear(predicted_x, predicted_y, odom["heading"] + yaw * 0.3):
                        try:
                            blocked_cell = fresh_grid.odom_to_grid(predicted_x, predicted_y)
                            current_cell = fresh_grid.odom_to_grid(odom["x"], odom["y"])
                            previous_cells = {fresh_grid.odom_to_grid(x, y)
                                              for x, y in avoided_points}
                        except (ValueError, AttributeError):
                            blocked_cell = current_cell = None
                            previous_cells = set()
                        emit_agent_event(
                            "navigation.progress", task_id=task_id,
                            phase="blocked_step", reason="body_clearance_unavailable",
                            pose_odom_xy=[round(odom["x"], 3), round(odom["y"], 3)],
                            predicted_odom_xy=[round(predicted_x, 3), round(predicted_y, 3)],
                            predicted_heading=round(odom["heading"] + yaw * 0.3, 3),
                            command={"vx": round(vx, 3), "vy": round(vy, 3),
                                     "yaw": round(yaw, 3)},
                            goal_odom_xy=[round(goal[0], 3), round(goal[1], 3)],
                            reroute=bool(blocked_cell is not None and blocked_cell != current_cell
                                         and blocked_cell not in previous_cells),
                        )
                        if (blocked_cell is not None and blocked_cell != current_cell
                                and blocked_cell not in previous_cells):
                            go2.move_velocity(0, 0, 0)
                            avoided_points.append((predicted_x, predicted_y))
                            path = None
                            continue
                        return self._finish("blocked", "body_clearance_unavailable",
                                            "下一步机身空间不足，已停止", task_id)
                go2.move_velocity(vx, vy, yaw, deadman_s=0.3)
                last_command = {"vx": round(vx, 3), "vy": round(vy, 3),
                                "yaw": round(yaw, 3)}
                await asyncio.sleep(0.1)
            return self._finish(
                "failed", "navigation_timeout", "导航超时，已停止", task_id,
                diagnostics={
                    "sensor": getattr(go2, "sensor_diagnostics", None),
                    "last_command": last_command,
                    "replan_count": replan_count,
                    "displacement_m": round(math.hypot(
                        self.last_trusted_pose["x"] - start_pose["x"],
                        self.last_trusted_pose["y"] - start_pose["y"]), 3)
                        if self.last_trusted_pose and start_pose else None,
                })
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

    def _plan_path(self, loc: dict, grid_obj, odom: dict,
                   avoided_points: list[tuple[float, float]] | None = None):
        try:
            start = grid_obj.odom_to_grid(odom["x"], odom["y"])
            raw_goal = grid_obj.odom_to_grid(loc["x"], loc["y"])
        except ValueError:
            return None
        if not grid_obj.is_free(*start):
            return None
        keepout = loc.get("keepout")
        path_grid = grid_obj.grid
        if keepout:
            path_grid = grid_obj.grid.copy()
            for gy in range(path_grid.shape[0]):
                for gx in range(path_grid.shape[1]):
                    wx, wy = grid_obj.grid_to_odom(gx, gy)
                    if math.hypot(wx - keepout[0], wy - keepout[1]) < keepout[2]:
                        path_grid[gy, gx] = True
        if avoided_points:
            if path_grid is grid_obj.grid:
                path_grid = path_grid.copy()
            for wx, wy in avoided_points:
                try:
                    ix, iy = grid_obj.odom_to_grid(wx, wy)
                except ValueError:
                    continue
                path_grid[iy, ix] = True
        if path_grid[start[1], start[0]]:
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
                path = astar(path_grid, start, candidate)
                if path:
                    goal = candidate
                    break
            else:
                goal = None
        path = astar(path_grid, start, goal) if goal is not None else None
        if path is None and keepout and loc.get("face_target") is not None:
            # The live map may mark the original stop cell occupied as the
            # robot approaches. Choose a new reachable stop from this map.
            from cloud.go2.perception.rgb_lidar import select_reachable_stop
            stop = select_reachable_stop(grid_obj, odom, loc["face_target"])
            if stop is not None:
                goal = grid_obj.odom_to_grid(stop["x"], stop["y"])
                path = astar(path_grid, start, goal)
        if path is None:
            return None
        wx, wy = grid_obj.grid_to_odom(*goal)
        return path, grid_obj, (wx, wy, goal != raw_goal)

    async def _confirm_stopped(self, goal, heading):
        go2.move_velocity(0, 0, 0)
        previous_stamp = getattr(go2, "odom_received_at", None)
        previous_pose = go2.odom
        samples = 0
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline and samples < 3:
            await asyncio.sleep(0.1)
            stamp = getattr(go2, "odom_received_at", None)
            if (not getattr(go2, "is_connected", False) or stamp is None
                    or time.monotonic() - stamp > POSE_STALE_S
                    or getattr(go2, "odom_frame_id", None) != "odom"):
                return False
            if stamp == previous_stamp:
                continue
            pose = go2.odom
            if (not pose or not all(isinstance(pose.get(key), (int, float))
                                    and math.isfinite(pose[key]) for key in ("x", "y", "heading"))):
                return False
            self.last_trusted_pose = pose.copy()
            near_goal = math.hypot(pose["x"] - goal[0], pose["y"] - goal[1]) < ARRIVAL_THRESHOLD
            heading_ok = (heading is None or
                          abs(_normalize_angle(pose["heading"] - heading)) <= HEADING_THRESHOLD)
            still = math.hypot(pose["x"] - previous_pose["x"],
                               pose["y"] - previous_pose["y"]) <= 0.02
            samples = samples + 1 if near_goal and heading_ok and still else 0
            previous_stamp, previous_pose = stamp, pose
        return samples == 3

    def _finish(self, status: str, reason: str | None, message: str, task_id: str | None,
                diagnostics: dict | None = None) -> dict:
        result = {"task_id": task_id, "status": status, "reason": reason, "message": message,
                  "last_trusted_pose": self.last_trusted_pose, "actual_goal": self.actual_goal}
        if diagnostics is not None:
            result["diagnostics"] = diagnostics
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
    front_only: bool = True,
) -> tuple[float, float]:
    """Find a path lookahead; holonomic motion may follow a point beside the body."""
    if not path:
        return goal_loc["x"], goal_loc["y"]
    # Replanning and odom updates can move the robot past the first few path
    # cells. Never select a cell behind the nearest point on this path.
    nearest_index = min(
        range(len(path)),
        key=lambda index: sum((coordinate - robot_coordinate) ** 2
                              for coordinate, robot_coordinate in zip(
                                  grid_obj.grid_to_odom(*path[index]),
                                  (robot_x, robot_y))),
    )
    for ix, iy in path[nearest_index + 1:]:
        wx, wy = grid_obj.grid_to_odom(ix, iy)
        dx, dy = wx - robot_x, wy - robot_y
        dist = math.sqrt(dx * dx + dy * dy)
        if dist < LOOKAHEAD_DIST:
            continue
        # 只选前方点（与当前朝向点积 > 0，排除身后点）
        if not front_only or math.cos(robot_heading) * dx + math.sin(robot_heading) * dy > 0:
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
