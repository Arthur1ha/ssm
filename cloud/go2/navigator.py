import asyncio
import math
import time
from enum import Enum
from typing import Optional

from cloud.go2.connection import go2
from cloud.go2 import spatial_memory

ARRIVAL_THRESHOLD  = 0.3
HEADING_THRESHOLD  = 0.25
WALK_SPEED         = 0.3
TURN_SPEED         = 0.5
KP_YAW             = 0.8
STUCK_CHECK_INTERVAL = 2.0
STUCK_DISTANCE     = 0.05
STUCK_BACKUP_TIME  = 1.0
MAX_RETRIES        = 3
TRAJ_INTERVAL      = 5.0


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
        try:
            result = await self._navigate_to(loc)
        finally:
            self.mode   = NavMode.IDLE
            self.target = None
        return result

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
        odom = go2.odom
        if odom:
            last_pos = {"x": odom["x"], "y": odom["y"]}

        while True:
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

            if abs(heading_error) > HEADING_THRESHOLD:
                sign = 1.0 if heading_error > 0 else -1.0
                go2.move_velocity(0.0, 0.0, TURN_SPEED * sign)
            else:
                vx   = min(WALK_SPEED, dist * 0.8)
                vyaw = KP_YAW * heading_error
                go2.move_velocity(vx, 0.0, vyaw)

            now = time.monotonic()

            if now - last_stuck_check >= STUCK_CHECK_INTERVAL:
                moved = math.sqrt(
                    (odom["x"] - last_pos["x"]) ** 2 +
                    (odom["y"] - last_pos["y"]) ** 2
                )
                if moved < STUCK_DISTANCE:
                    go2.move_velocity(0, 0, 0)
                    if retries >= MAX_RETRIES:
                        return f"无法到达「{loc['name']}」，已重试 {retries} 次"
                    await asyncio.sleep(0.3)
                    go2.move_velocity(-WALK_SPEED, 0.0, 0.0)
                    await asyncio.sleep(STUCK_BACKUP_TIME)
                    go2.move_velocity(0, 0, 0)
                    return await self._navigate_to(loc, retries + 1)
                last_pos         = {"x": odom["x"], "y": odom["y"]}
                last_stuck_check = now

            if now - last_traj_tick >= TRAJ_INTERVAL:
                spatial_memory.record_trajectory_tick(odom)
                last_traj_tick = now

            await asyncio.sleep(0.1)

    async def _patrol_loop(self, stops: list[str]) -> None:
        while True:
            for name in stops:
                self.target = name
                loc = await spatial_memory.find_location(name)
                if loc is not None:
                    await self._navigate_to(loc)
                await asyncio.sleep(2.0)


def _normalize_angle(angle: float) -> float:
    while angle > math.pi:
        angle -= 2.0 * math.pi
    while angle < -math.pi:
        angle += 2.0 * math.pi
    return angle


navigator = Navigator()
