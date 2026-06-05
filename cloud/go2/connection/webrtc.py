import asyncio
import logging

from unitree_webrtc_connect import UnitreeWebRTCConnection, WebRTCConnectionMethod
from unitree_webrtc_connect.constants import RTC_TOPIC, SPORT_CMD

from cloud.go2.connection.fsm import Go2FSM
from cloud.go2.connection.sensors import Go2Sensors
from cloud.go2.connection.video import Go2Video

_VALID_LED_COLORS = frozenset({"white", "red", "yellow", "blue", "green", "cyan", "purple"})


class Go2Connection(Go2FSM, Go2Sensors, Go2Video):
    def __init__(self) -> None:
        super().__init__()
        self._conn: UnitreeWebRTCConnection | None = None
        self.is_connected: bool = False

    # ── 生命周期 ──────────────────────────────────────────────────────────

    async def connect(self, email: str, password: str, serial: str, region: str = "cn") -> None:
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

        self._conn.datachannel.pub_sub.subscribe(RTC_TOPIC["LF_SPORT_MOD_STATE"], self._on_state)
        self._conn.datachannel.pub_sub.subscribe(RTC_TOPIC["ROBOTODOM"],           self._on_odom)
        self._conn.datachannel.pub_sub.subscribe(RTC_TOPIC["LOW_STATE"],           self._on_low_state)
        self._conn.datachannel.pub_sub.subscribe(RTC_TOPIC["ULIDAR_ARRAY"],        self._on_voxel_map)
        await self._conn.datachannel.disableTrafficSaving(True)

        self.is_connected = True
        self.fsm_state = "standing"
        logging.info("[Go2] WebRTC 连接成功")

        video_track = None
        for t in self._conn.pc.getTransceivers():
            if t.kind == "video" and t.receiver and t.receiver.track:
                video_track = t.receiver.track
                break

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
            # 先关闭 PeerConnection，取消所有 pending aioice STUN transactions
            try:
                if hasattr(self._conn, "pc") and self._conn.pc:
                    await self._conn.pc.close()
            except Exception:
                pass
            try:
                await self._conn.disconnect()
            except Exception:
                pass
            self._conn = None
        logging.info("[Go2] 已断开")

    # ── 命令发送 ──────────────────────────────────────────────────────────

    async def send_command(self, cmd: str, params: dict | None = None) -> None:
        if not self.is_connected or not self._conn:
            raise RuntimeError("Go2 not connected")
        api_id = SPORT_CMD.get(cmd)
        if api_id is None:
            raise ValueError(f"Unknown command: {cmd}")
        options: dict = {"api_id": api_id}
        if params:
            options["parameter"] = params
        await self._conn.datachannel.pub_sub.publish_request_new(RTC_TOPIC["SPORT_MOD"], options)

        next_state = self.fsm_next(cmd)
        if next_state is not None:
            self.fsm_state = next_state
        if next_state == "executing":
            self._schedule_exec_reset()

    async def switch_mode(self, mode: str) -> None:
        if not self.is_connected or not self._conn:
            raise RuntimeError("Go2 not connected")
        await self._conn.datachannel.pub_sub.publish_request_new(
            RTC_TOPIC["MOTION_SWITCHER"],
            {"api_id": 1002, "parameter": {"name": mode}},
        )

    def move_velocity(self, vx: float, vy: float, vyaw: float) -> None:
        if not self.is_connected or not self._conn:
            raise RuntimeError("Go2 not connected")
        self._conn.datachannel.pub_sub.publish_without_callback(
            RTC_TOPIC["WIRELESS_CONTROLLER"],
            data={"lx": -vy, "ly": vx, "rx": -vyaw, "ry": 0},
        )

    async def balance_stand(self) -> None:
        if not self.is_connected or not self._conn:
            raise RuntimeError("Go2 not connected")
        await self._conn.datachannel.pub_sub.publish_request_new(
            RTC_TOPIC["SPORT_MOD"],
            {"api_id": SPORT_CMD["BalanceStand"]},
        )
        self.fsm_state = "standing"

    async def set_obstacle_avoidance(self, enabled: bool) -> None:
        if not self.is_connected or not self._conn:
            raise RuntimeError("Go2 not connected")
        await self._conn.datachannel.pub_sub.publish_request_new(
            RTC_TOPIC["OBSTACLES_AVOID"],
            {"api_id": 1001, "parameter": {"enable": int(enabled)}},
        )

    async def set_led(self, color: str = "white", duration: int = 60) -> None:
        if color not in _VALID_LED_COLORS:
            raise ValueError(
                f"Unknown color {color!r}, choose from: {', '.join(sorted(_VALID_LED_COLORS))}"
            )
        if not self.is_connected or not self._conn:
            raise RuntimeError("Go2 not connected")
        await self._conn.datachannel.pub_sub.publish_request_new(
            RTC_TOPIC["VUI"],
            {"api_id": 1001, "parameter": {"color": color, "time": duration}},
        )


go2 = Go2Connection()
