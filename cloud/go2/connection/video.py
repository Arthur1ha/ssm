"""Fresh camera frames with a single replaceable encoding slot per connection."""
import asyncio
import base64
import io
import logging
import time

logger = logging.getLogger(__name__)

JPEG_QUALITY = 90
PREVIEW_JPEG_QUALITY = 70
PREVIEW_MAX_WIDTH = 640
ENCODE_INTERVAL_S = 0.1
VIDEO_MAX_AGE_S = 3.0


def _encode_frame(frame):
    """Both JPEGs share one conversion; never run conversion/encoding on the loop."""
    from PIL import Image
    image = frame.to_image()
    original = io.BytesIO()
    image.save(original, format="JPEG", quality=JPEG_QUALITY)
    preview_image = image
    if image.width > PREVIEW_MAX_WIDTH:
        preview_image = image.resize(
            (PREVIEW_MAX_WIDTH, max(1, round(image.height * PREVIEW_MAX_WIDTH / image.width))),
            Image.Resampling.BILINEAR,
        )
    preview = io.BytesIO()
    preview_image.save(preview, format="JPEG", quality=PREVIEW_JPEG_QUALITY)
    return original.getvalue(), preview.getvalue(), image.width, image.height


class Go2Video:
    def __init__(self) -> None:
        super().__init__()
        self._latest_frame: bytes | None = None
        self._latest_preview_frame: bytes | None = None
        self._latest_frame_meta: dict | None = None
        self._frame_sequence = 0
        self._frame_ready: asyncio.Event | None = None
        self._video_task: asyncio.Task | None = None
        self._video_track_ended = False
        self._video_error: str | None = None
        self._video_received_at: float | None = None
        self._video_dropped_frames = 0

    def _get_frame_event(self) -> asyncio.Event:
        if self._frame_ready is None:
            self._frame_ready = asyncio.Event()
        return self._frame_ready

    async def _stop_video(self) -> None:
        task, self._video_task = self._video_task, None
        session = getattr(self, '_odom_session_id', None)
        if task is not None and task is not asyncio.current_task():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        if session != getattr(self, '_odom_session_id', None) or self._video_task is not None:
            return
        self._latest_frame = self._latest_preview_frame = None
        self._latest_frame_meta = None
        self._video_track_ended = True
        if self._frame_ready is not None:
            self._frame_ready.set()

    async def _consume_video(self, track) -> None:
        logger.info("[Go2/Video] 采集开始 JPEG=%d preview=%d max_fps=%.0f",
                    JPEG_QUALITY, PREVIEW_JPEG_QUALITY, 1 / ENCODE_INTERVAL_S)
        session = getattr(self, '_odom_session_id', None)
        self._video_track_ended = False
        self._video_error = None
        self._video_received_at = None
        self._video_dropped_frames = 0
        ready = asyncio.Event()
        pending = None
        ended = False

        def current():
            return self.is_connected and session == getattr(self, '_odom_session_id', None)

        async def receive():
            nonlocal pending, ended
            try:
                while current():
                    frame = await track.recv()
                    if not current():
                        break
                    received_at = time.monotonic()
                    self._video_received_at = received_at
                    if pending is not None:
                        self._video_dropped_frames += 1
                    pending = (frame, {"captured_at": time.time(), "received_at": received_at,
                        "odom_session_id": session,
                        "pose_at_capture": getattr(self, '_odom', {}).copy(),
                        "pose_received_at": getattr(self, '_odom_received_at', None)})
                    ready.set()
                    # A buffered receiver may complete immediately; let the encoder run.
                    await asyncio.sleep(0)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if current():
                    self._video_error = type(exc).__name__
                    logger.warning("[Go2/Video] track 结束: %s", exc)
            finally:
                ended = True
                ready.set()

        receiver = asyncio.create_task(receive(), name='go2-video-receive')
        last_encode = 0.0
        try:
            while current():
                await ready.wait()
                delay = ENCODE_INTERVAL_S - (time.monotonic() - last_encode)
                if delay > 0:
                    await asyncio.sleep(delay)
                ready.clear()
                item, pending = pending, None
                if item is None:
                    if ended:
                        break
                    continue
                frame, metadata = item
                last_encode = time.monotonic()
                try:
                    original, preview, width, height = await asyncio.to_thread(_encode_frame, frame)
                except Exception as exc:
                    if current():
                        self._video_error = type(exc).__name__
                    logger.debug("[Go2/Video] 帧编码失败，跳过", exc_info=True)
                    if ended:
                        break
                    continue
                if not current():
                    break
                self._frame_sequence += 1
                self._latest_frame = original
                self._latest_preview_frame = preview
                self._latest_frame_meta = {**metadata, "frame_id": self._frame_sequence,
                                           "width": width, "height": height}
                self._get_frame_event().set()
                if ended and pending is None:
                    break
        finally:
            receiver.cancel()
            await asyncio.gather(receiver, return_exceptions=True)
            if session == getattr(self, '_odom_session_id', None):
                self._video_track_ended = True
                self._get_frame_event().set()
            logger.info("[Go2/Video] 采集停止")

    def live_frame_snapshot(self, *, preview=False) -> tuple[bytes, dict] | None:
        snapshot = self.latest_frame_snapshot()
        if (not getattr(self, 'is_connected', False) or self._video_track_ended
                or snapshot is None):
            return None
        frame, meta = snapshot
        received = meta.get('received_at')
        if (meta.get('odom_session_id') != getattr(self, '_odom_session_id', None)
                or type(received) not in (int, float)
                or not 0 <= time.monotonic() - received <= VIDEO_MAX_AGE_S):
            return None
        if preview and self._latest_preview_frame is not None:
            frame = self._latest_preview_frame
        return frame, meta

    def video_diagnostics(self) -> dict:
        now = time.monotonic()
        encoded = (self._latest_frame_meta or {}).get('received_at')
        return {"receiving": bool(self._video_task is not None and not self._video_task.done()),
                "track_ended": self._video_track_ended, "error": self._video_error,
                "received_age_s": None if self._video_received_at is None else now - self._video_received_at,
                "frame_age_s": None if encoded is None else now - encoded,
                "dropped_frames": self._video_dropped_frames,
                "frame_id": (self._latest_frame_meta or {}).get('frame_id')}

    async def mjpeg_generator(self):
        session = getattr(self, '_odom_session_id', None)
        event = self._get_frame_event()
        previous = None
        while self.is_connected and session == getattr(self, '_odom_session_id', None):
            event.clear()
            snapshot = self.live_frame_snapshot(preview=True)
            if snapshot is not None:
                frame, meta = snapshot
                if meta['frame_id'] != previous:
                    previous = meta['frame_id']
                    age = max(0, int((time.monotonic() - meta['received_at']) * 1000))
                    headers = (f"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: {len(frame)}\r\n"
                               f"X-Frame-Id: {previous}\r\nX-Frame-Timestamp: {meta['captured_at']}\r\n"
                               f"X-Frame-Age-Ms: {age}\r\n\r\n").encode('ascii')
                    yield headers + frame + b'\r\n'
                    continue
            if self._video_track_ended or (previous is not None and snapshot is None):
                return
            try:
                await asyncio.wait_for(event.wait(), timeout=1.0)
            except asyncio.TimeoutError:
                pass

    def latest_frame_b64(self) -> str | None:
        if self._latest_frame is None:
            return None
        return base64.b64encode(self._latest_frame).decode()

    def latest_frame_snapshot(self) -> tuple[bytes, dict] | None:
        """Original-quality evidence; consumers apply their own freshness limits."""
        if self._latest_frame is None or self._latest_frame_meta is None:
            return None
        return self._latest_frame, self._latest_frame_meta.copy()
