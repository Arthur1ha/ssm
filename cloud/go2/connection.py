import asyncio
import base64
import logging

from unitree_webrtc_connect import UnitreeWebRTCConnection, WebRTCConnectionMethod
from unitree_webrtc_connect.constants import RTC_TOPIC, SPORT_CMD

_FSM_AVAILABLE: dict[str, list[str]] = {
    "offline":    [],
    "connecting": [],
    "lying":      ["StandUp"],
    "standing":   ["StandDown", "Move", "Hello", "Stretch", "Dance1", "Dance2"],
    "moving":     ["Move", "StopMove"],
    "executing":  ["StopMove"],
}

_FSM_NEXT: dict[str, dict[str, str]] = {
    "lying":     {"StandUp":   "standing"},
    "standing":  {"StandDown": "lying", "Move": "moving",
                  "Hello": "executing", "Stretch": "executing",
                  "Dance1": "executing", "Dance2": "executing"},
    "moving":    {"StopMove": "standing"},
    "executing": {"StopMove": "standing"},
}

_EXEC_RESET_DELAY = 12.0  # seconds before auto-reset executing → standing


class Go2Connection:
    def __init__(self) -> None:
        self._conn: UnitreeWebRTCConnection | None = None
        self.is_connected: bool = False
        self._latest_frame: bytes | None = None
        self._robot_state: dict = {}
        self._state_queues: list[asyncio.Queue] = []
        self._odom: dict = {}
        self._low_state: dict = {}
        self._odom_queues: list[asyncio.Queue] = []
        self._frame_ready: asyncio.Event | None = None
        self.fsm_state: str = "offline"
        self._exec_reset_task: asyncio.Task | None = None

    @property
    def available_actions(self) -> list[str]:
        return _FSM_AVAILABLE.get(self.fsm_state, [])

    def _get_frame_event(self) -> asyncio.Event:
        if self._frame_ready is None:
            self._frame_ready = asyncio.Event()
        return self._frame_ready

    async def connect(self, email: str, password: str, serial: str, region: str = "cn") -> None:
        """建立 Remote 模式 WebRTC 连接。应作为 asyncio.create_task() 调用。"""
        self.fsm_state = "connecting"
        if self._conn:
            await self._conn.disconnect()

        self._conn = UnitreeWebRTCConnection(
            WebRTCConnectionMethod.Remote,
            serialNumber=serial,
            username=email,
            password=password,
            region=region,
            device_type="Go2",
        )

        try:
            await self._conn.connect()
        except Exception as exc:
            self.is_connected = False
            logging.error("[Go2] 连接失败: %s", exc)
            raise

        self._conn.datachannel.pub_sub.subscribe(
            RTC_TOPIC["LF_SPORT_MOD_STATE"], self._on_state
        )
        await self._conn.datachannel.disableTrafficSaving(True)
        self._conn.datachannel.pub_sub.subscribe(
            RTC_TOPIC["ROBOTODOM"], self._on_odom
        )
        self._conn.datachannel.pub_sub.subscribe(
            RTC_TOPIC["LOW_STATE"], self._on_low_state
        )
        self.is_connected = True
        self.fsm_state = "standing"
        logging.info("[Go2] WebRTC 连接成功")

        # track 事件在 connect() 内部触发，此处直接从 transceiver 取 track 消费
        video_track = None
        for t in self._conn.pc.getTransceivers():
            if t.kind == "video" and t.receiver and t.receiver.track:
                video_track = t.receiver.track
                break

        # 通知 Go2 开始推送视频（不发这条指令机器狗默认不传视频）
        self._conn.video.switchVideoChannel(True)

        if video_track:
            asyncio.create_task(self._consume_video(video_track))
            logging.info("[Go2] 视频 track 已找到，开始采集")
        else:
            logging.warning("[Go2] 未找到视频 track，画面不可用")

    async def disconnect(self) -> None:
        self.is_connected = False
        self.fsm_state = "offline"
        if self._exec_reset_task and not self._exec_reset_task.done():
            self._exec_reset_task.cancel()
        self._frame_ready = None
        if self._conn:
            await self._conn.disconnect()
            self._conn = None
        logging.info("[Go2] 已断开")

    def _on_state(self, msg: dict) -> None:
        data = msg.get("data", {})
        inner = data.get("data", data) if isinstance(data, dict) else {}
        self._robot_state = {
            "mode":        inner.get("mode"),
            "body_height": inner.get("body_height"),
            "velocity":    inner.get("velocity"),
        }
        for q in self._state_queues:
            try:
                q.put_nowait(self._robot_state.copy())
            except asyncio.QueueFull:
                pass

    def _on_odom(self, msg: dict) -> None:
        import math
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

    def _on_low_state(self, msg: dict) -> None:
        data = msg.get("data", {})
        self._low_state = {
            "battery_soc": data.get("bms_state", {}).get("soc"),
            "power_v":     data.get("power_v"),
            "imu_rpy":     data.get("imu_state", {}).get("rpy", [0.0, 0.0, 0.0]),
            "foot_force":  data.get("foot_force", [0, 0, 0, 0]),
        }

    async def switch_mode(self, mode: str) -> None:
        if not self.is_connected or not self._conn:
            raise RuntimeError("Go2 not connected")
        await self._conn.datachannel.pub_sub.publish_request_new(
            RTC_TOPIC["MOTION_SWITCHER"],
            {"api_id": 1002, "parameter": {"name": mode}},
        )

    def _schedule_exec_reset(self) -> None:
        if self._exec_reset_task and not self._exec_reset_task.done():
            self._exec_reset_task.cancel()

        async def _reset():
            await asyncio.sleep(_EXEC_RESET_DELAY)
            if self.fsm_state == "executing":
                self.fsm_state = "standing"

        self._exec_reset_task = asyncio.create_task(_reset())

    async def send_command(self, cmd: str, params: dict | None = None) -> None:
        if not self.is_connected or not self._conn:
            raise RuntimeError("Go2 not connected")
        api_id = SPORT_CMD.get(cmd)
        if api_id is None:
            raise ValueError(f"Unknown command: {cmd}")

        if cmd not in self.available_actions:
            raise ValueError(
                f"'{cmd}' 在当前状态 '{self.fsm_state}' 下不可用，"
                f"可用动作: {self.available_actions}"
            )

        options: dict = {"api_id": api_id}
        if params:
            options["parameter"] = params
        await self._conn.datachannel.pub_sub.publish_request_new(
            RTC_TOPIC["SPORT_MOD"], options
        )

        next_state = _FSM_NEXT.get(self.fsm_state, {}).get(cmd)
        if next_state is not None:
            self.fsm_state = next_state
        if next_state == "executing":
            self._schedule_exec_reset()

    async def _consume_video(self, track) -> None:
        import io, time
        loop = asyncio.get_event_loop()
        logging.info("[Go2] 视频采集开始")
        last_encode = 0.0
        while self.is_connected:
            try:
                frame = await track.recv()   # 必须持续 drain，否则 aiortc 缓冲区积压
            except Exception as e:
                logging.warning("[Go2] 视频 track 结束: %s", e)
                break
            now = time.monotonic()
            if now - last_encode < 0.04:  # ~25fps
                continue
            try:
                img = frame.to_image()
                def encode():
                    buf = io.BytesIO()
                    img.save(buf, format="JPEG", quality=40)
                    return buf.getvalue()
                self._latest_frame = await loop.run_in_executor(None, encode)
                last_encode = now
                self._get_frame_event().set()
            except Exception as e:
                logging.debug("[Go2] 帧编码失败，跳过: %s", e)
        logging.info("[Go2] 视频采集停止")

    async def mjpeg_generator(self):
        """Async generator，有新帧时立即 yield，无重复帧。"""
        event = self._get_frame_event()
        while self.is_connected:
            try:
                await asyncio.wait_for(event.wait(), timeout=1.0)
            except asyncio.TimeoutError:
                continue
            event.clear()
            frame = self._latest_frame
            if frame:
                yield (
                    b"--frame\r\nContent-Type: image/jpeg\r\n\r\n"
                    + frame
                    + b"\r\n"
                )

    def new_state_queue(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=20)
        self._state_queues.append(q)
        return q

    def remove_state_queue(self, q: asyncio.Queue) -> None:
        try:
            self._state_queues.remove(q)
        except ValueError:
            pass

    @property
    def odom(self) -> dict:
        return self._odom.copy()

    @property
    def low_state(self) -> dict:
        return self._low_state.copy()

    def new_odom_queue(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=20)
        self._odom_queues.append(q)
        return q

    def remove_odom_queue(self, q: asyncio.Queue) -> None:
        try:
            self._odom_queues.remove(q)
        except ValueError:
            pass

    def latest_frame_b64(self) -> str | None:
        if self._latest_frame is None:
            return None
        return base64.b64encode(self._latest_frame).decode()


go2 = Go2Connection()
