import asyncio
import base64
import io
import logging
import time


class Go2Video:
    def __init__(self) -> None:
        super().__init__()
        self._latest_frame: bytes | None = None
        self._frame_ready: asyncio.Event | None = None

    def _get_frame_event(self) -> asyncio.Event:
        if self._frame_ready is None:
            self._frame_ready = asyncio.Event()
        return self._frame_ready

    async def _consume_video(self, track) -> None:
        loop = asyncio.get_event_loop()
        logging.info("[Go2] 视频采集开始")
        last_encode = 0.0
        while self.is_connected:
            try:
                frame = await track.recv()  # 必须持续 drain，否则 aiortc 缓冲区积压
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
