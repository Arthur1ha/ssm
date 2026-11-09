"""
导航控制器，负责将机器人从当前位置引导到目标地点。
只在新鲜同帧的已知自由空间规划，所有退出路径停止。
"""
import asyncio
from cloud.agents.common.runner import invoke_blocking
import logging
import math
import time
from enum import Enum
from typing import Callable, Optional

from cloud.go2.state import runtime_for
from cloud.go2.connection import go2
from cloud.agents.go2.memory import spatial as spatial_memory
from cloud.go2.navigation.astar import astar
from cloud.go2.control import control_lease, LeaseConflict
from cloud.go2.actions import (VELOCITY_LIMITS, MOVE_DEFAULT_SPEED,
    MOVE_DISTANCE_TOLERANCE_M, MOVE_ANGLE_TOLERANCE_RAD)
from cloud.observability import emit_agent_event

logger = logging.getLogger(__name__)

NAV_TIMEOUT          = 60.0   # s
POSE_STALE_S         = 1.0    # s
MAP_STALE_S          = 2.0    # s
STOP_CONFIRMATION_S = 3.0  # same bounded window for fresh stationary feedback
TELEMETRY_RECOVERY_S = 3.0    # s; hold still while fresh odom/map packets resume
ARRIVAL_THRESHOLD    = 0.3    # m
HEADING_THRESHOLD    = 0.25   # rad，对齐朝向误差上限
WALK_SPEED           = 0.3    # m/s
TURN_SPEED           = 0.5    # rad/s
KP_YAW               = 0.8
MAX_RETRIES          = 6
MIN_LINEAR_VEL       = 0.1    # m/s，路径跟随最低线速度
MOTION_PROGRESS_TIMEOUT_S = 5.0
LOOKAHEAD_DIST       = 0.6    # m，前瞻距离
REPLAN_INTERVAL      = 1.0    # s，重规划间隔
MAX_PATH_DEVIATION   = 0.8    # m，偏离路径超过此值立即重规划
MAX_SPIN_TIME        = 10.0   # s，初始转向超时
# 偏角超过此值时纯转向，否则边走边修正
PATH_HEADING_INHIBIT = 0.5    # rad (~28°)


class _MotionProgress:
    """Count commanded time per dimension; phase changes cannot erase stalls."""

    def __init__(self):
        self.reset()

    def reset(self):
        self.anchors = {}
        self.active_s = {'translation': 0., 'rotation': 0.}
        self.last_at = None
        self.last_phase = None
        self.overall_at = None

    def observe(self, pose, vx, vy, now):
        phase = 'translation' if vx or vy else 'rotation'
        if self.last_at is None:
            self.anchors = {kind: pose.copy() for kind in self.active_s}
            self.overall_at = now
        else:
            self.active_s[self.last_phase] += max(0., now - self.last_at)
        for kind, anchor in self.anchors.items():
            delta = (math.hypot(pose['x']-anchor['x'], pose['y']-anchor['y'])
                     if kind == 'translation' else
                     abs(_normalize_angle(pose['heading']-anchor['heading'])))
            if delta >= (.02 if kind == 'translation' else .04):
                self.anchors[kind] = pose.copy()
                self.active_s[kind] = 0.
                self.overall_at = now
        self.last_at, self.last_phase = now, phase
        stalled_for = max(self.active_s[phase], now - self.overall_at)
        if stalled_for >= MOTION_PROGRESS_TIMEOUT_S:
            return {'motion_phase': phase, 'without_progress_s': round(stalled_for, 3)}
        return None


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
        self._life_tokens = {}
        self.last_stop_diagnostics = {}

    @property
    def state(self) -> dict:
        return {"mode": self.mode, "task_id": self.task_id, "status": self.status,
                "reason": self.reason, "message": self.message, "target": self.target,
                "actual_goal": self.actual_goal, "patrol_stops": self.patrol_stops,
                "last_trusted_pose": self.last_trusted_pose,
                "owner_session_id": self.owner_session_id}

    def _begin(self, name: str, owner_session_id: str, task: asyncio.Task) -> str:
        import uuid
        runtime = runtime_for(go2)
        if runtime is not None and (runtime.active_reason() or go2.body_ready_reason() or go2.body_state not in {'standing', 'moving'} or go2.pending_action):
            raise ValueError(runtime.active_reason() or 'body_not_ready')
        self.stop()
        self.task_id = uuid.uuid4().hex
        if runtime is not None and runtime.data['life_state'] == 'idle':
            self._life_tokens[self.task_id] = runtime.begin_life('play', owner_session_id, self.task_id)
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
        if runtime is not None:
            runtime.commit('navigation_accepted', navigation_active=True)
            accepted_id, accepted_epoch = self.task_id, runtime.data['epoch']
            def finished(_task):
                if self.task_id == accepted_id and runtime.data['epoch'] == accepted_epoch:
                    runtime.commit('navigation_finished', navigation_active=False)
                    from cloud.go2.mode.free_explore import drive
                    drive.resume_after_user()
            task.add_done_callback(finished)
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
        try:
            return self._begin(name, owner_session_id, task)
        except BaseException:
            task.cancel()
            raise

    async def go_to(self, name: str, owner_session_id: str = "system") -> dict:
        self._begin(name, owner_session_id, asyncio.current_task())
        return await self._run_to(name)

    async def move_relative(self, direction: str, *, distance_m: float | None = None,
                            angle_deg: float | None = None, speed: float = MOVE_DEFAULT_SPEED,
                            context) -> dict:
        """Reuse navigation's velocity loop and stop proof for an odom goal."""
        from cloud.go2.tools.executor import validate_execution_context
        validate_execution_context(context)
        snapshot, reason = self.snapshot()
        if reason:
            return {"result": "blocked", "reason": reason,
                    "message": "定位或地图尚未就绪，未开始移动。"}
        start = snapshot[0].copy()
        session = getattr(go2, "_odom_session_id", None)
        if (not session or start.get("session_id") != session
                or not all(type(start.get(k)) in (int, float) and math.isfinite(start[k])
                           for k in ("x", "y", "heading"))):
            return {"result": "blocked", "reason": "localization_unavailable",
                    "message": "缺少当前定位会话的可靠位置，未开始移动。"}
        turning = direction in {"turn_left", "turn_right"}
        offsets = {"forward": 0., "backward": math.pi, "left": math.pi/2, "right": -math.pi/2}
        axis = start["heading"] + offsets.get(direction, 0.)
        sign = -1 if direction == "turn_right" else 1
        heading = _normalize_angle(start["heading"] + sign * math.radians(angle_deg)) if turning else start["heading"]
        x = start["x"] if turning else start["x"] + distance_m * math.cos(axis)
        y = start["y"] if turning else start["y"] + distance_m * math.sin(axis)
        loc = {"name": "相对移动", "x": x, "y": y, "heading": heading,
               "session_id": session, "frame_id": "odom", "relative_motion": True,
               "speed_limit": speed, "holonomic": True,
               "arrival_tolerance_m": MOVE_DISTANCE_TOLERANCE_M,
               "context": context}
        control_lease.acquire(context)
        task = asyncio.current_task()
        self._begin("相对移动", context.session_id, task)
        receipt = await self._run_to("相对移动", loc)
        owner = control_lease.snapshot()
        if (owner is None or owner.owner_session_id != context.session_id
                or getattr(go2, "_odom_session_id", None) != session):
            stop = {"result": "accepted", "reason": "control_or_session_changed"}
        else:
            stop = await self.stop_and_confirm(owner_session_id=context.session_id)
        final = self.last_trusted_pose if stop.get("result") == "ok" else receipt.get("last_trusted_pose")
        if not final or final.get("session_id") != session:
            final = None
        distance = ((final["x"] - start["x"]) * math.cos(axis)
                    + (final["y"] - start["y"]) * math.sin(axis)) if final else None
        angle = sign * math.degrees(_normalize_angle(final["heading"] - start["heading"])) if final else None
        if turning and angle is not None and angle < -90 and angle_deg > 90:
            angle += 360
        measured = angle if turning else distance
        requested = angle_deg if turning else distance_m
        tolerance = math.degrees(MOVE_ANGLE_TOLERANCE_RAD) if turning else MOVE_DISTANCE_TOLERANCE_M
        completed = (receipt["status"] == "succeeded" and stop.get("result") == "ok"
                     and measured is not None and abs(measured - requested) <= tolerance)
        status = "ok" if completed else "cancelled" if receipt["status"] == "cancelled" else "blocked"
        reason = None if completed else receipt.get("reason") or "distance_not_reached"
        if stop.get("result") != "ok":
            status, reason = "accepted", "stop_unconfirmed"
        labels = {"forward": "向前移动", "backward": "向后移动", "left": "向左移动",
                  "right": "向右移动", "turn_left": "向左转", "turn_right": "向右转"}
        unit = "度" if turning else "米"
        message = (f"{labels[direction]}约{measured:.2f}{unit}，目标{requested:g}{unit}。"
                   if measured is not None else "未取得可靠的实际位移反馈。")
        if completed:
            message += "已达到本次目标并确认停稳。"
        elif stop.get("result") != "ok":
            message += "尚未确认机身停稳，不能算完成。"
        else:
            detail = ("后续未观察到继续移动" if reason == "motion_not_observed"
                      and measured is not None and measured > tolerance else receipt.get('message') or reason)
            message += f"未完成本次目标，已确认停稳；{detail}。"
        result = {"result": status, "reason": reason, "message": message,
                  "task_id": receipt.get("task_id"), "direction": direction,
                  "requested_distance_m": distance_m, "distance_m": round(distance, 3) if distance is not None else None,
                  "requested_angle_deg": angle_deg, "angle_deg": round(angle, 2) if turning and angle is not None else None,
                  "start_pose": start, "last_trusted_pose": final, "stop": stop,
                  "navigation": receipt, "confirmation": "odometry_and_stop" if completed else None}
        if task.cancelling():
            raise asyncio.CancelledError
        return result

    async def go_to_coordinate(self, x: float, y: float, owner_session_id: str = "system",
                               heading: float | None = None,
                               keepout: tuple[float, float, float] | None = None,
                               speed_limit: float | None = None,
                               minimum_command_speed: float | None = None,
                               holonomic: bool = False,
                               face_target: tuple[float, float] | None = None,
                               stop_when_near_target_m: float | None = None,
                               stop_cell_tolerance_m: float | None = None,
                               face_target_bias_rad: float | Callable[[], float] = 0.0,
                               arrival_tolerance_m: float | None = None,
                               bounds: tuple[float, float, float, float] | None = None) -> dict:
        task = asyncio.current_task()
        self._begin("exploration target", owner_session_id, task)
        return await self._run_to("exploration target", {"name": "exploration target", "x": x, "y": y,
                                                          "heading": heading, "frame_id": "odom",
                                                          "keepout": keepout,
                                                          "bounds": bounds,
                                                          "speed_limit": speed_limit,
                                                          "minimum_command_speed": minimum_command_speed,
                                                          "holonomic": holonomic,
                                                          "face_target": face_target,
                                                          "stop_when_near_target_m": stop_when_near_target_m,
                                                          "stop_cell_tolerance_m": stop_cell_tolerance_m,
                                                          "face_target_bias_rad": face_target_bias_rad,
                                                          "arrival_tolerance_m": arrival_tolerance_m,
                                                          "session_id": getattr(go2, "_odom_session_id", None)})

    async def _run_to(self, name: str, loc: dict | None = None) -> dict:
        task = asyncio.current_task()
        task_id = self.task_id
        try:
            if loc is None:
                loc = await spatial_memory.find_location(name)
            if loc is None:
                return self._finish("blocked", "location_not_found", f"找不到地点「{name}」", task_id)
            if not loc.get("session_id") or loc["session_id"] != getattr(go2, "_odom_session_id", None):
                return self._finish("blocked", "map_mismatch", "地点不属于当前 odom 会话", task_id)
            if (loc.get("frame_id") != "odom" or
                    not all(isinstance(loc.get(k), (int, float)) and math.isfinite(loc[k]) for k in ("x", "y")) or
                    (loc.get("heading") is not None and not math.isfinite(loc["heading"]))):
                return self._finish("blocked", "invalid_location", "地点坐标无效", task_id)
            bounds = loc.get("bounds")
            if bounds is not None and (len(bounds) != 4 or not all(math.isfinite(v) for v in bounds)
                                       or bounds[0] >= bounds[2] or bounds[1] >= bounds[3]):
                return self._finish("blocked", "invalid_search_scope", "搜索范围无效", task_id)
            speed_limit = loc.get("speed_limit")
            if speed_limit is not None and (not isinstance(speed_limit, (int, float))
                                            or not math.isfinite(speed_limit)
                                            or not MIN_LINEAR_VEL <= speed_limit <= (
                                                min(VELOCITY_LIMITS.values()) if loc.get("relative_motion") else WALK_SPEED)):
                return self._finish("blocked", "invalid_speed_limit", "导航速度限制无效", task_id)
            precise_tolerance = loc.get("arrival_tolerance_m")
            if precise_tolerance is not None and (type(precise_tolerance) not in (int, float)
                    or not math.isfinite(precise_tolerance) or not .05 <= precise_tolerance <= ARRIVAL_THRESHOLD):
                return self._finish("blocked", "invalid_arrival_tolerance", "导航到点容差无效", task_id)
            command_floor = loc.get("minimum_command_speed")
            if command_floor is None:
                command_floor = MIN_LINEAR_VEL
            if (type(command_floor) not in (int, float) or not math.isfinite(command_floor)
                    or not MIN_LINEAR_VEL <= command_floor <= (speed_limit or WALK_SPEED)):
                return self._finish("blocked", "invalid_command_floor", "导航指令下限无效", task_id)
            self.status = "executing"
            if task_id in self.task_records:
                self.task_records[task_id]["status"] = "executing"
            context = loc.get("context")
            deadline = time.monotonic() + min(NAV_TIMEOUT,
                max(0., context.expires_at - time.time()) if context and context.expires_at else NAV_TIMEOUT)
            last_replan = 0.0
            replan_count = 0
            path = None
            grid_obj = None
            goal = None
            sensor_gap_since = None
            sensor_gap_reason = None
            last_command = None
            start_pose = None
            progress = _MotionProgress()
            avoided_points = []

            motion_started = False
            async def send_motion(vx, vy, yaw, pose):
                """Require progress in the dimension actually being commanded."""
                nonlocal last_command, motion_started
                if loc.get("relative_motion") and not motion_started:
                    from cloud.go2.tools.executor import execute_action
                    ready = await execute_action("EnterMoveControl", context=context)
                    if ready.get("result") not in {"ok", "accepted"}:
                        return {"reason": ready.get("error") or "motion_start_rejected", "start": ready}
                    started = await execute_action("Move", {"x": vx, "y": vy, "z": yaw}, context=context)
                    if started.get("result") not in {"ok", "accepted"}:
                        return {"reason": started.get("error") or "motion_start_rejected", "start": started}
                    motion_started = True
                    # Gateway calls yield; recheck sensor and task ownership
                    # before beginning the periodic stream of this same action.
                    _, changed = self._snapshot()
                    if changed or loc['session_id'] != getattr(go2, '_odom_session_id', None):
                        return {"reason": changed or "map_mismatch"}
                    control_lease.heartbeat_session(self.owner_session_id)
                stalled = progress.observe(pose, vx, vy, time.monotonic())
                if stalled:
                    return {**stalled, 'last_command': last_command}
                go2.move_velocity(vx, vy, yaw, deadman_s=.3)
                last_command = {"vx": round(vx, 3), "vy": round(vy, 3), "yaw": round(yaw, 3)}
                return None

            while time.monotonic() < deadline:
                if self.owner_session_id != "system":
                    try:
                        control_lease.heartbeat_session(self.owner_session_id)
                    except LeaseConflict:
                        return self._finish("cancelled", "control_lease_lost", "运动租约已失效", task_id)
                snapshot, reason = self._snapshot()
                if reason:
                    progress.reset()
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
                            sensor_gap_reason = reason
                            logger.warning("[Go2/Nav] telemetry paused reason=%s diagnostics=%s",
                                           reason, getattr(go2, "sensor_diagnostics", None))
                        if now - sensor_gap_since < TELEMETRY_RECOVERY_S:
                            await asyncio.sleep(0.1)
                            continue
                    diagnostics = {
                        "sensor": getattr(go2, "sensor_diagnostics", None),
                        "latest_reason": reason,
                        "last_command": last_command,
                        "gap_s": round(now - sensor_gap_since, 3) if sensor_gap_since is not None else None,
                        "displacement_m": round(math.hypot(
                            self.last_trusted_pose["x"] - start_pose["x"],
                            self.last_trusted_pose["y"] - start_pose["y"]), 3)
                            if self.last_trusted_pose and start_pose else None,
                    }
                    return self._finish("failed", sensor_gap_reason if recoverable else reason,
                                        "定位或地图数据中断，已停止导航", task_id,
                                        diagnostics=diagnostics)
                if sensor_gap_since is not None:
                    logger.info("[Go2/Nav] telemetry resumed after %.2fs; replanning",
                                time.monotonic() - sensor_gap_since)
                    sensor_gap_since = None
                    sensor_gap_reason = None
                    path = None
                if loc["session_id"] != getattr(go2, "_odom_session_id", None):
                    return self._finish("blocked", "map_mismatch", "定位会话已变化，导航已停止", task_id)
                odom, fresh_grid = snapshot
                if loc.get("relative_motion") and odom.get("session_id") != loc["session_id"]:
                    return self._finish("blocked", "map_mismatch", "定位会话已变化，移动已停止", task_id)
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
                    planned_pose = odom.copy()
                    planned = await invoke_blocking(self._plan_path, loc, fresh_grid, odom, avoided_points)
                    # Planning yields control. Its input may be stale or the
                    # device/session/owner may have changed before it returns.
                    latest, latest_reason = self._snapshot()
                    if latest_reason:
                        go2.move_velocity(0, 0, 0)
                        progress.reset()
                        path = None
                        await asyncio.sleep(.1)
                        continue
                    odom, fresh_grid = latest
                    if loc['session_id'] != getattr(go2, '_odom_session_id', None):
                        return self._finish('blocked', 'map_mismatch', '定位会话已变化，导航已停止', task_id)
                    if self.owner_session_id != 'system':
                        try:
                            control_lease.heartbeat_session(self.owner_session_id)
                        except LeaseConflict:
                            return self._finish('cancelled', 'control_lease_lost', '运动租约已失效', task_id)
                    if (math.hypot(odom['x']-planned_pose['x'], odom['y']-planned_pose['y']) > .10
                            or abs(_normalize_angle(odom['heading']-planned_pose['heading'])) > .15):
                        go2.move_velocity(0, 0, 0)
                        progress.reset()
                        path = None
                        continue
                    self.last_trusted_pose = odom.copy()
                    if keepout and math.hypot(odom['x']-keepout[0], odom['y']-keepout[1]) < keepout[2]:
                        return self._finish('blocked', 'target_keepout_breached', '目标安全距离不足', task_id)
                    if planned is None:
                        reason = "body_clearance_unavailable" if avoided_points else "no_known_free_path"
                        message = ("地图未确认机身可通行路线，已停止" if avoided_points
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
                stop_cell_tolerance = loc.get("stop_cell_tolerance_m")
                if (near_radius is not None and stop_cell_tolerance is not None
                        and 0 < stop_cell_tolerance <= 0.5
                        and dist <= stop_cell_tolerance):
                    body_clear = getattr(fresh_grid, "is_body_clear", None)
                    if body_clear is None or not body_clear(odom["x"], odom["y"], odom["heading"]):
                        return self._finish("blocked", "body_clearance_unavailable",
                                            "地图未确认当前停步位置安全，已停止", task_id)
                    self.actual_goal = {"x": odom["x"], "y": odom["y"], "adjusted": True}
                    if await self._confirm_stopped((odom["x"], odom["y"], True), None):
                        return self._finish("succeeded", None, "已在目标停靠点附近停稳", task_id)
                    return self._finish("failed", "arrival_unconfirmed", "未能确认停稳抵达", task_id)
                arrival_tolerance = ARRIVAL_THRESHOLD / 2 if near_radius is not None else ARRIVAL_THRESHOLD
                if precise_tolerance is not None:
                    arrival_tolerance = min(arrival_tolerance, precise_tolerance)
                if dist < arrival_tolerance:
                    if loc.get("heading") is not None:
                        heading_error = _normalize_angle(loc["heading"] - odom["heading"])
                        heading_tolerance = MOVE_ANGLE_TOLERANCE_RAD if loc.get("relative_motion") else HEADING_THRESHOLD
                        if abs(heading_error) > heading_tolerance:
                            details = self._turn_details(fresh_grid, odom, loc["heading"])
                            if not details["clear"]:
                                return self._finish("blocked", "turn_clearance_unavailable",
                                                    "转向扫掠空间未确认，已停止", task_id,
                                                    diagnostics=details)
                            turn_limit = min(TURN_SPEED, speed_limit) if loc.get("relative_motion") else TURN_SPEED
                            stalled = await send_motion(0, 0, max(-turn_limit, min(turn_limit, KP_YAW * heading_error)), odom)
                            if stalled:
                                return self._finish("blocked", stalled.get("reason") or "motion_not_observed",
                                                    "设备约束拒绝启动转向" if stalled.get("reason") else
                                                    "转向指令未产生可确认的转动，已停止", task_id,
                                                    diagnostics=stalled)
                            await asyncio.sleep(0.1)
                            continue
                    if await self._confirm_stopped(goal, loc.get("heading"), arrival_tolerance):
                        suffix = "停靠点" if goal[2] else f"「{name}」"
                        return self._finish("succeeded", None, f"已到达{suffix}", task_id)
                    return self._finish("failed", "arrival_unconfirmed", "未能确认停稳抵达", task_id)
                if _distance_to_path(path, grid_obj, odom["x"], odom["y"]) > MAX_PATH_DEVIATION:
                    go2.move_velocity(0, 0, 0)
                    progress.reset()
                    path = None
                    continue
                lx, ly = _find_lookahead_on_path(path, grid_obj, odom["x"], odom["y"], odom["heading"],
                                                  {"x": goal[0], "y": goal[1]},
                                                  front_only=not loc.get("holonomic", False))
                if loc.get("relative_motion"):
                    lx, ly = loc["x"], loc["y"]
                heading_error = _normalize_angle(math.atan2(ly - odom["y"], lx - odom["x"]) - odom["heading"])
                if loc.get("holonomic"):
                    speed = min(speed_limit or WALK_SPEED, max(command_floor, dist * 0.8))
                    vx, vy = speed * math.cos(heading_error), speed * math.sin(heading_error)
                    if loc.get('minimum_command_speed') is not None:
                        # A vector norm floor alone leaves small diagonal axes
                        # ineffective. Prefer the dominant body axis instead of
                        # amplifying both axes beyond the requested norm limit.
                        if abs(vx) < command_floor or abs(vy) < command_floor:
                            if abs(vx) >= abs(vy):
                                vx, vy = math.copysign(speed, vx), 0.
                            else:
                                vx, vy = 0., math.copysign(speed, vy)
                    face_target = loc.get("face_target")
                    if face_target is None:
                        yaw = (max(-TURN_SPEED, min(TURN_SPEED, KP_YAW * _normalize_angle(
                            loc["heading"] - odom["heading"]))) if loc.get("relative_motion") else 0.0)
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
                    vx = min(speed_limit or WALK_SPEED, max(command_floor, dist * 0.8))
                    vy = 0.0
                    yaw = max(-TURN_SPEED, min(TURN_SPEED, KP_YAW * heading_error))
                body_clear = getattr(fresh_grid, "is_body_clear", None)
                if body_clear is not None:
                    if not body_clear(odom["x"], odom["y"], odom["heading"]):
                        return self._finish("blocked", "body_clearance_unavailable",
                                            "地图未确认当前机身周围可通行空间，已停止", task_id)
                    if vx == 0 and vy == 0 and yaw != 0:
                        details = self._turn_details(fresh_grid, odom, odom["heading"] + heading_error)
                        if not details["clear"]:
                            return self._finish("blocked", "turn_clearance_unavailable",
                                                "转向扫掠空间未确认，已停止", task_id,
                                                diagnostics=details)
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
                        if (not loc.get("relative_motion") and blocked_cell is not None and blocked_cell != current_cell
                                and blocked_cell not in previous_cells):
                            go2.move_velocity(0, 0, 0)
                            progress.reset()
                            avoided_points.append((predicted_x, predicted_y))
                            path = None
                            continue
                        return self._finish("blocked", "body_clearance_unavailable",
                                            "地图未确认下一步机身可通行空间，已停止", task_id)
                stalled = await send_motion(vx, vy, yaw, odom)
                if stalled:
                    return self._finish("blocked", stalled.get("reason") or "motion_not_observed",
                                        "设备约束拒绝启动移动" if stalled.get("reason") else
                                        "运动指令未产生可确认的位移，已停止", task_id,
                                        diagnostics=stalled)
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
                owner = control_lease.snapshot() if loc and loc.get("relative_motion") else None
                if not loc or not loc.get("relative_motion") or (
                        owner and owner.owner_session_id == self.owner_session_id
                        and loc["session_id"] == getattr(go2, "_odom_session_id", None)):
                    go2.move_velocity(0, 0, 0)
            except (RuntimeError, ValueError):
                logger.warning("[Go2/Nav] stop packet could not be sent")
            if self._task is task:
                runtime = runtime_for(go2)
                if runtime is not None:
                    runtime.commit('navigation_finished', navigation_active=False)
                self._task = None
                self.mode = NavMode.IDLE
                self.owner_session_id = None
                self.current_path = None
                self.current_grid_obj = None
            if runtime_for(go2) is not None:
                from cloud.go2.mode.free_explore import drive
                drive.resume_after_user()

    def snapshot(self):
        """Read one consistent, fresh map/pose pair without rebuilding voxels."""
        return self._snapshot()

    async def confirm_stopped(self, goal, heading=None):
        return await self._confirm_stopped(goal, heading)

    async def stop_and_confirm(self, owner_session_id=None):
        if owner_session_id is not None and self.owner_session_id not in {None, owner_session_id}:
            return {"result": "blocked", "status": "blocked", "reason": "control_owner_mismatch"}
        self.stop(owner_session_id=None)
        pose = go2.odom
        if not getattr(go2, 'is_connected', False) or not pose:
            return {"result": "accepted", "status": "failed", "reason": "stop_unconfirmed"}
        try:
            # Cancellation confirms rest wherever braking finished. Arrival
            # separately supplies its goal and heading below; the request pose
            # is not an arrival target and may change while the robot stops.
            stopped = await self._confirm_stopped(None, None)
        except (RuntimeError, ValueError, KeyError) as exc:
            stopped = False
            self.last_stop_diagnostics = {"reason": "stop_command_or_pose_error",
                                          "error_type": type(exc).__name__}
        return {"result": "ok" if stopped else "accepted",
                "status": "succeeded" if stopped else "failed",
                "reason": None if stopped else "stop_unconfirmed",
                "diagnostics": self.last_stop_diagnostics.copy()}

    @staticmethod
    def _turn_details(grid, pose, heading):
        sweep = getattr(grid, 'turn_sweep_details', None)
        if callable(sweep):
            return sweep(pose['x'], pose['y'], pose['heading'], heading)
        check = getattr(grid, 'is_turn_clear', None)
        return {"clear": bool(callable(check) and check(pose['x'], pose['y'])),
                "blockers": ["turn_evidence_unavailable"]}

    def _snapshot(self):
        if not getattr(go2, "is_connected", False):
            return None, "disconnected"
        pose_age = getattr(go2, "odom_received_at", None)
        map_age = getattr(go2, "voxel_received_at", None)
        now = time.monotonic()
        if (type(pose_age) not in (int, float) or not math.isfinite(pose_age)
                or not 0 <= now - pose_age <= POSE_STALE_S):
            return None, "localization_unavailable"
        if (type(map_age) not in (int, float) or not math.isfinite(map_age)
                or not 0 <= now - map_age <= MAP_STALE_S):
            logger.info("[Go2/Nav] map unavailable: %s",
                        "no_voxel_packet" if map_age is None else
                        "voxel_stale age=%.1fs" % (now - map_age))
            return None, "map_unavailable"
        pose_frame = getattr(go2, "odom_frame_id", None)
        map_frame = getattr(go2, "voxel_frame_id", None)
        if not pose_frame or not map_frame or pose_frame != map_frame or pose_frame != "odom":
            return None, "frame_mismatch"
        odom = go2.odom
        if not odom or not all(type(odom.get(k)) in (int, float) and math.isfinite(odom[k])
                               for k in ("x", "y", "heading")):
            return None, "localization_unavailable"
        try:
            grid = go2.occupancy_grid
        except (ValueError, TypeError) as exc:
            logger.info("[Go2/Nav] map unavailable: grid_build_%s", type(exc).__name__)
            return None, "map_unavailable"
        if grid is None or grid.frame_id != map_frame or not getattr(grid, "has_free_space_evidence", False):
            logger.info("[Go2/Nav] map unavailable: %s", "grid_missing" if grid is None else
                        "grid_frame_mismatch" if grid.frame_id != map_frame else
                        "no_free_space_evidence")
            return None, "map_unavailable"
        if getattr(grid, "session_id", getattr(go2, "_odom_session_id", None)) != getattr(go2, "_odom_session_id", None):
            return None, "map_mismatch"
        if (now - getattr(grid, "observed_at", map_age) > MAP_STALE_S
                or map_age - getattr(grid, "observed_at", map_age) > .3):
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
        if loc.get("relative_motion"):
            # A relative direction request follows a straight segment and its
            # exact odom endpoint; never round it to a cell or detour sideways.
            if not grid_obj.is_free(*raw_goal):
                return None
            return [start, raw_goal], grid_obj, (loc["x"], loc["y"], False)
        keepout = loc.get("keepout")
        path_grid = grid_obj.grid
        bounds = loc.get("bounds")
        if bounds is not None:
            import numpy as np
            from cloud.go2.navigation.occupancy_grid import TURN_CLEARANCE_M
            path_grid = path_grid.copy()
            yy, xx = np.indices(path_grid.shape)
            wx = grid_obj.origin[0] + (xx + .5) * grid_obj.resolution
            wy = grid_obj.origin[1] + (yy + .5) * grid_obj.resolution
            path_grid |= ((wx < bounds[0] + TURN_CLEARANCE_M) | (wx > bounds[2] - TURN_CLEARANCE_M)
                          | (wy < bounds[1] + TURN_CLEARANCE_M) | (wy > bounds[3] - TURN_CLEARANCE_M))
        if keepout:
            path_grid = path_grid.copy()
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
        goal_tolerance = loc.get('arrival_tolerance_m') or ARRIVAL_THRESHOLD
        if not grid_obj.is_free(*goal):
            # Only known free cells reachable from the robot may be stop points.
            candidates = []
            radius = math.ceil(goal_tolerance / grid_obj.resolution)
            for dy in range(-radius, radius + 1):
                for dx in range(-radius, radius + 1):
                    candidate = (raw_goal[0] + dx, raw_goal[1] + dy)
                    if not grid_obj.is_free(*candidate):
                        continue
                    wx, wy = grid_obj.grid_to_odom(*candidate)
                    if math.hypot(wx - loc["x"], wy - loc["y"]) <= goal_tolerance:
                        candidates.append((math.hypot(wx - loc["x"], wy - loc["y"]), candidate))
            for _, candidate in sorted(candidates):
                path = astar(path_grid, start, candidate)
                if path:
                    goal = candidate
                    break
            else:
                goal = None
        path = astar(path_grid, start, goal) if goal is not None else None
        if (path is None and keepout and loc.get("face_target") is not None
                and loc.get('arrival_tolerance_m') is None):
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
        if goal == raw_goal:
            # Grid cells select a route, not a replacement coordinate. In
            # particular, a short forward move must not acquire a lateral
            # offset (and unwanted yaw) solely through cell-center rounding.
            from cloud.go2.navigation.occupancy_grid import TURN_CLEARANCE_M
            x, y = loc['x'], loc['y']
            body_clear = getattr(grid_obj, 'is_body_clear', None)
            heading = loc.get('heading') if loc.get('heading') is not None else odom.get('heading', 0.)
            within_bounds = bounds is None or (bounds[0]+TURN_CLEARANCE_M <= x <= bounds[2]-TURN_CLEARANCE_M
                                              and bounds[1]+TURN_CLEARANCE_M <= y <= bounds[3]-TURN_CLEARANCE_M)
            outside_keepout = not keepout or math.hypot(x-keepout[0], y-keepout[1]) >= keepout[2]
            if within_bounds and outside_keepout and callable(body_clear) and body_clear(x, y, heading):
                wx, wy = x, y
        return path, grid_obj, (wx, wy, goal != raw_goal)

    async def _confirm_stopped(self, goal, heading, arrival_tolerance=ARRIVAL_THRESHOLD):
        started = time.monotonic()
        self.last_stop_diagnostics = {"reason": "awaiting_fresh_pose", "samples": 0}
        go2.move_velocity(0, 0, 0)
        previous_stamp = getattr(go2, "odom_received_at", None)
        previous_pose = go2.odom
        expected_session = getattr(go2, "_odom_session_id", None)
        samples = 0
        deadline = started + STOP_CONFIRMATION_S

        def diagnostic(reason, **values):
            self.last_stop_diagnostics = {"reason": reason, "samples": samples,
                "elapsed_s": round(time.monotonic() - started, 3), **values}

        while time.monotonic() < deadline and samples < 3:
            await asyncio.sleep(0.1)
            stamp = getattr(go2, "odom_received_at", None)
            if not getattr(go2, "is_connected", False):
                diagnostic("disconnected")
                return False
            if getattr(go2, "_odom_session_id", None) != expected_session:
                diagnostic("map_session_changed")
                return False
            if getattr(go2, "odom_frame_id", None) != "odom":
                diagnostic("odom_frame_mismatch")
                return False
            age = None if stamp is None else time.monotonic() - stamp
            if age is None or not 0 <= age <= POSE_STALE_S:
                # Zero velocity is already sent. A receiver scheduling gap may
                # recover within the original stop window, but earns no proof.
                samples = 0
                previous_stamp, previous_pose = stamp, None
                diagnostic("localization_unavailable", pose_age_s=age)
                continue
            if stamp == previous_stamp:
                diagnostic("awaiting_new_pose", pose_age_s=age)
                continue
            pose = go2.odom
            if (not pose or not all(type(pose.get(key)) in (int, float)
                                    and math.isfinite(pose[key]) for key in ("x", "y", "heading"))):
                diagnostic("invalid_pose")
                return False
            self.last_trusted_pose = pose.copy()
            if not previous_pose:
                previous_stamp, previous_pose = stamp, pose
                diagnostic("awaiting_stable_pose")
                continue
            near_goal = goal is None or math.hypot(pose["x"] - goal[0], pose["y"] - goal[1]) < arrival_tolerance
            heading_ok = (heading is None or
                          abs(_normalize_angle(pose["heading"] - heading)) <= HEADING_THRESHOLD)
            still = math.hypot(pose["x"] - previous_pose["x"],
                               pose["y"] - previous_pose["y"]) <= 0.02
            still = still and abs(_normalize_angle(pose["heading"] - previous_pose["heading"])) <= .03
            samples = samples + 1 if near_goal and heading_ok and still else 0
            diagnostic("confirmed" if samples == 3 else "awaiting_stable_pose",
                       near_goal=near_goal, heading_ok=heading_ok, still=still)
            previous_stamp, previous_pose = stamp, pose
        if samples != 3:
            logger.warning("[Go2/Nav] stop confirmation unavailable: %s", self.last_stop_diagnostics)
        return samples == 3

    def _finish(self, status: str, reason: str | None, message: str, task_id: str | None,
                diagnostics: dict | None = None) -> dict:
        runtime = runtime_for(go2)
        token = self._life_tokens.pop(task_id, None)
        if runtime is not None and token:
            runtime.finish_life(token)
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
        runtime = runtime_for(go2)
        token = self._life_tokens.pop(self.task_id, None)
        if runtime is not None and token:
            runtime.finish_life(token)
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




navigator = Navigator()
