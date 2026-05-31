import asyncio
import logging

from unitree_webrtc_connect import UnitreeWebRTCConnection, WebRTCConnectionMethod
from unitree_webrtc_connect.constants import RTC_TOPIC, SPORT_CMD


class Go2Connection:
    def __init__(self) -> None:
        self._conn: UnitreeWebRTCConnection | None = None
        self.is_connected: bool = False
        self._latest_frame: bytes | None = None
        self._robot_state: dict = {}
        self._state_queues: list[asyncio.Queue] = []

    async def connect(self, email: str, password: str, serial: str, region: str = "cn") -> None:
        """建立 Remote 模式 WebRTC 连接。应作为 asyncio.create_task() 调用。"""
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
        self.is_connected = True
        logging.info("[Go2] WebRTC 连接成功")

        # track 事件在 connect() 内部触发，此处直接从 transceiver 取 track 消费
        video_track = None
        for t in self._conn.pc.getTransceivers():
            if t.kind == "video" and t.receiver and t.receiver.track:
                video_track = t.receiver.track
                break

        if video_track:
            asyncio.create_task(self._consume_video(video_track))
            logging.info("[Go2] 视频 track 已找到，开始采集")
        else:
            logging.warning("[Go2] 未找到视频 track，画面不可用")

    async def disconnect(self) -> None:
        self.is_connected = False
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

    async def send_command(self, cmd: str, params: dict | None = None) -> None:
        if not self.is_connected or not self._conn:
            raise RuntimeError("Go2 not connected")
        api_id = SPORT_CMD.get(cmd)
        if api_id is None:
            raise ValueError(f"Unknown command: {cmd}")
        options: dict = {"api_id": api_id}
        if params:
            options["parameter"] = params
        await self._conn.datachannel.pub_sub.publish_request_new(
            RTC_TOPIC["SPORT_MOD"], options
        )

    async def _consume_video(self, track) -> None:
        import io
        logging.info("[Go2] 视频采集开始")
        while self.is_connected:
            try:
                frame = await track.recv()
                buf = io.BytesIO()
                frame.to_image().save(buf, format="JPEG", quality=75)
                self._latest_frame = buf.getvalue()
            except Exception as e:
                logging.warning("[Go2] 视频帧读取结束: %s", e)
                break
        logging.info("[Go2] 视频采集停止")

    async def mjpeg_generator(self):
        """Async generator，每 ~33ms yield 一帧 MJPEG multipart 数据。"""
        while self.is_connected:
            if self._latest_frame:
                yield (
                    b"--frame\r\nContent-Type: image/jpeg\r\n\r\n"
                    + self._latest_frame
                    + b"\r\n"
                )
            await asyncio.sleep(0.033)

    def new_state_queue(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=20)
        self._state_queues.append(q)
        return q

    def remove_state_queue(self, q: asyncio.Queue) -> None:
        try:
            self._state_queues.remove(q)
        except ValueError:
            pass


go2 = Go2Connection()
