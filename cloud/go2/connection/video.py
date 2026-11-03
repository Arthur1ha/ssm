import asyncio
import base64
import io
import logging
import time

logger = logging.getLogger(__name__)


class Go2Video:
    def __init__(self) -> None:
        super().__init__()
        self._latest_frame: bytes | None = None
        self._latest_frame_meta: dict | None = None
        self._frame_sequence = 0
        self._frame_ready: asyncio.Event | None = None

    def _get_frame_event(self) -> asyncio.Event:
        if self._frame_ready is None:
            self._frame_ready = asyncio.Event()
        return self._frame_ready

    async def _consume_video(self, track) -> None:
        loop = asyncio.get_event_loop()
        logger.info("[Go2/Video] 采集开始")
        last_encode = 0.0
        while self.is_connected:
            try:
                frame = await track.recv()  # 必须持续 drain，否则 aiortc 缓冲区积压
                captured_at = time.time()
                received_at = time.monotonic()
                pose_at_capture = getattr(self, "_odom", {}).copy()
                pose_received_at = getattr(self, "_odom_received_at", None)
            except Exception as e:
                logger.warning("[Go2/Video] track 结束: %s", e)
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
                self._frame_sequence += 1
                self._latest_frame_meta = {"frame_id": self._frame_sequence,
                                           "captured_at": captured_at,
                                           "received_at": received_at,
                                           "odom_session_id": getattr(self, "_odom_session_id", None),
                                           "pose_at_capture": pose_at_capture,
                                           "pose_received_at": pose_received_at,
                                           "width": img.width, "height": img.height}
                last_encode = now
                self._get_frame_event().set()
            except Exception as e:
                logging.debug("[Go2] 帧编码失败，跳过: %s", e)
        logger.info("[Go2/Video] 采集停止")

    async def mjpeg_generator(self):
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

    def latest_frame_b64(self) -> str | None:
        if self._latest_frame is None:
            return None
        return base64.b64encode(self._latest_frame).decode()

    def latest_frame_snapshot(self) -> tuple[bytes, dict] | None:
        """Return one JPEG and its capture metadata from the same encoded frame."""
        if self._latest_frame is None or self._latest_frame_meta is None:
            return None
        return self._latest_frame, self._latest_frame_meta.copy()
