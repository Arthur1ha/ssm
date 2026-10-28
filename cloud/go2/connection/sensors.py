import asyncio
import math
import time


class Go2Sensors:
    def __init__(self) -> None:
        super().__init__()
        self._robot_state: dict = {}
        self._last_state_received_at: float | None = None
        self._odom: dict = {}
        self._odom_received_at: float | None = None
        self._odom_source_ts = None
        self._odom_frame_id: str | None = None
        self._odom_interval_s: float | None = None
        self._odom_count = 0
        self._low_state: dict = {}
        self._low_state_received_at: float | None = None
        self._low_state_interval_s: float | None = None
        self._low_state_count = 0
        self._voxel_raw: dict | None = None
        self._voxel_received_at: float | None = None
        self._voxel_source_ts = None
        self._voxel_frame_id: str | None = None
        self._voxel_interval_s: float | None = None
        self._voxel_count = 0
        self._global_path: list[dict] = []   # 机器人自身规划的全局路径点
        self._state_queues: list[asyncio.Queue] = []
        self._odom_queues: list[asyncio.Queue] = []

    def reset_sensor_epoch(self) -> None:
        """Discard all pose/map observations when a WebRTC session changes."""
        self._odom = {}
        self._odom_received_at = None
        self._odom_source_ts = None
        self._odom_frame_id = None
        self._odom_interval_s = None
        self._odom_count = 0
        self._voxel_raw = None
        self._voxel_received_at = None
        self._voxel_source_ts = None
        self._voxel_frame_id = None
        self._voxel_interval_s = None
        self._voxel_count = 0
        self._global_path = []
        self._low_state = {}
        self._low_state_received_at = None
        self._low_state_interval_s = None
        self._low_state_count = 0

    # ── 回调 ──────────────────────────────────────────────────────────────

    def _on_state(self, msg: dict) -> None:
        data = msg.get("data", {})
        inner = data.get("data", data) if isinstance(data, dict) else {}
        self._last_state_received_at = time.monotonic()
        progress = float(inner.get("progress") or 0.0)
        self._fsm_on_progress(progress)
        self._fsm_on_posture(inner.get("body_height"))
        self._robot_state = {
            "mode":        inner.get("mode"),
            "progress":    progress,
            "body_height": inner.get("body_height"),
            "velocity":    inner.get("velocity"),
            "fsm_state":   self.fsm_state,
        }
        for q in self._state_queues:
            try:
                q.put_nowait(self._robot_state.copy())
            except asyncio.QueueFull:
                pass

    def _on_odom(self, msg: dict) -> None:
        data = msg.get("data", {})
        if not isinstance(data, dict):
            data = {}
        header = data.get("header") or {}
        pose = data.get("pose") or {}
        if not isinstance(header, dict) or not isinstance(pose, dict):
            self._odom = {}
            self._odom_received_at = None
            self._odom_frame_id = None
            return
        pos = pose.get("position") or {}
        ori = pose.get("orientation") or {}
        if not isinstance(pos, dict) or not isinstance(ori, dict):
            self._odom = {}
            self._odom_received_at = None
            self._odom_frame_id = None
            return
        try:
            x, y = float(pos["x"]), float(pos["y"])
            qx, qy, qz, qw = (float(ori[k]) for k in ("x", "y", "z", "w"))
            values = (x, y, qx, qy, qz, qw)
            if not all(math.isfinite(v) for v in values) or sum(q*q for q in (qx, qy, qz, qw)) < 0.5:
                raise ValueError("invalid pose")
        except (KeyError, TypeError, ValueError):
            self._odom = {}
            self._odom_received_at = None
            self._odom_frame_id = None
            return
        heading = math.atan2(2.0 * (qw * qz + qx * qy),
                             1.0 - 2.0 * (qy * qy + qz * qz))
        self._odom = {
            "x": x,
            "y": y,
            "heading": heading,
            "session_id": getattr(self, "_odom_session_id", None),
        }
        now = time.monotonic()
        if self._odom_received_at is not None:
            self._odom_interval_s = now - self._odom_received_at
        self._odom_received_at = now
        self._odom_count += 1
        self._odom_source_ts = header.get("stamp") or data.get("stamp")
        self._odom_frame_id = header.get("frame_id") or data.get("frame_id")
        for q in self._odom_queues:
            try:
                q.put_nowait(self._odom.copy())
            except asyncio.QueueFull:
                pass

    def _on_voxel_map(self, msg: dict) -> None:
        self._voxel_raw = msg
        now = time.monotonic()
        if self._voxel_received_at is not None:
            self._voxel_interval_s = now - self._voxel_received_at
        self._voxel_received_at = now
        self._voxel_count += 1
        data = msg.get("data", {})
        if not isinstance(data, dict):
            data = {}
        header = data.get("header") or {}
        if not isinstance(header, dict):
            header = {}
        self._voxel_source_ts = header.get("stamp") or data.get("stamp")
        self._voxel_frame_id = header.get("frame_id") or data.get("frame_id")

    def _on_global_path(self, msg: dict) -> None:
        """解析机器人内置导航的全局路径，存为 [{x, y}, ...] 列表。"""
        try:
            poses = msg.get("data", {}).get("poses", [])
            self._global_path = [
                {"x": p["pose"]["position"]["x"], "y": p["pose"]["position"]["y"]}
                for p in poses
                if isinstance(p, dict) and "pose" in p
            ]
        except Exception:
            self._global_path = []

    def _on_low_state(self, msg: dict) -> None:
        now = time.monotonic()
        if self._low_state_received_at is not None:
            self._low_state_interval_s = now - self._low_state_received_at
        self._low_state_received_at = now
        self._low_state_count += 1
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
    def voxel_received_at(self) -> float | None:
        return self._voxel_received_at

    @property
    def odom_received_at(self) -> float | None:
        return self._odom_received_at

    @property
    def odom_frame_id(self) -> str | None:
        return self._odom_frame_id

    @property
    def voxel_frame_id(self) -> str | None:
        return self._voxel_frame_id

    @property
    def sensor_diagnostics(self) -> dict:
        now = time.monotonic()
        voxel_data = (self._voxel_raw or {}).get("data") or {}
        if not isinstance(voxel_data, dict):
            voxel_data = {}
        def rate(interval):
            return None if interval is None or interval <= 0 else round(1.0 / interval, 2)
        return {
            "pose": {"frame_id": self._odom_frame_id, "source_ts": self._odom_source_ts,
                     "age_s": None if self._odom_received_at is None else now - self._odom_received_at,
                     "samples": self._odom_count, "latest_rate_hz": rate(self._odom_interval_s)},
            "voxel": {"frame_id": self._voxel_frame_id, "source_ts": self._voxel_source_ts,
                      "age_s": None if self._voxel_received_at is None else now - self._voxel_received_at,
                      "samples": self._voxel_count, "latest_rate_hz": rate(self._voxel_interval_s),
                      "origin": voxel_data.get("origin"), "resolution": voxel_data.get("resolution"),
                      "width": voxel_data.get("width")},
            "imu": {"age_s": None if self._low_state_received_at is None else now - self._low_state_received_at,
                    "samples": self._low_state_count, "latest_rate_hz": rate(self._low_state_interval_s)},
        }

    @property
    def global_path(self) -> list[dict]:
        return self._global_path

    @property
    def occupancy_grid(self):
        if self._voxel_raw is None:
            return None
        from cloud.go2.navigation.occupancy_grid import OccupancyGrid
        return OccupancyGrid(self._voxel_raw, robot_pose=self._odom)

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
