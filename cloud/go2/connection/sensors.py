import asyncio
import math


class Go2Sensors:
    def __init__(self) -> None:
        super().__init__()
        self._robot_state: dict = {}
        self._odom: dict = {}
        self._low_state: dict = {}
        self._voxel_raw: dict | None = None
        self._state_queues: list[asyncio.Queue] = []
        self._odom_queues: list[asyncio.Queue] = []

    # ── 回调 ──────────────────────────────────────────────────────────────

    def _on_state(self, msg: dict) -> None:
        data = msg.get("data", {})
        inner = data.get("data", data) if isinstance(data, dict) else {}
        progress = float(inner.get("progress") or 0.0)
        self._robot_state = {
            "mode":        inner.get("mode"),
            "progress":    progress,
            "body_height": inner.get("body_height"),
            "velocity":    inner.get("velocity"),
        }
        self._fsm_on_progress(progress)
        for q in self._state_queues:
            try:
                q.put_nowait(self._robot_state.copy())
            except asyncio.QueueFull:
                pass

    def _on_odom(self, msg: dict) -> None:
        data = msg.get("data", {})
        pose = data.get("pose", {})
        pos = pose.get("position", {})
        ori = pose.get("orientation", {})
        qx, qy, qz, qw = (
            ori.get("x", 0.0), ori.get("y", 0.0),
            ori.get("z", 0.0), ori.get("w", 1.0),
        )
        heading = math.atan2(2.0 * (qw * qz + qx * qy),
                             1.0 - 2.0 * (qy * qy + qz * qz))
        self._odom = {
            "x": pos.get("x", 0.0),
            "y": pos.get("y", 0.0),
            "heading": heading,
        }
        for q in self._odom_queues:
            try:
                q.put_nowait(self._odom.copy())
            except asyncio.QueueFull:
                pass

    def _on_voxel_map(self, msg: dict) -> None:
        self._voxel_raw = msg

    def _on_low_state(self, msg: dict) -> None:
        data = msg.get("data", {})
        self._low_state = {
            "battery_soc": data.get("bms_state", {}).get("soc"),
            "power_v":     data.get("power_v"),
            "imu_rpy":     data.get("imu_state", {}).get("rpy", [0.0, 0.0, 0.0]),
            "foot_force":  data.get("foot_force", [0, 0, 0, 0]),
        }

    # ── 数据属性 ──────────────────────────────────────────────────────────

    @property
    def odom(self) -> dict:
        return self._odom.copy()

    @property
    def low_state(self) -> dict:
        return self._low_state.copy()

    @property
    def voxel_raw(self) -> dict | None:
        return self._voxel_raw

    @property
    def occupancy_grid(self):
        if self._voxel_raw is None:
            return None
        from cloud.go2.navigation.occupancy_grid import OccupancyGrid
        return OccupancyGrid(self._voxel_raw)

    # ── 队列管理 ──────────────────────────────────────────────────────────

    def new_state_queue(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=20)
        self._state_queues.append(q)
        return q

    def remove_state_queue(self, q: asyncio.Queue) -> None:
        try:
            self._state_queues.remove(q)
        except ValueError:
            pass

    def new_odom_queue(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=20)
        self._odom_queues.append(q)
        return q

    def remove_odom_queue(self, q: asyncio.Queue) -> None:
        try:
            self._odom_queues.remove(q)
        except ValueError:
            pass
