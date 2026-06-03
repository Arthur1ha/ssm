import asyncio
import logging
import time
from typing import Callable, TypedDict

import cv2
import numpy as np

logger = logging.getLogger(__name__)

# ── 结构化输出类型 ─────────────────────────────────────────────────

class PersonInfo(TypedDict):
    detected: bool
    count: int


class FaceInfo(TypedDict):
    detected: bool
    count: int


class VisionFrame(TypedDict):
    ts: float
    persons: PersonInfo
    faces: FaceInfo
    changed: bool
    change_type: str  # "person_entered" | "person_left" | "count_changed" | "none"


# ── 检测器惰性初始化 ──────────────────────────────────────────────

_hog: cv2.HOGDescriptor | None = None
_face_cascade: cv2.CascadeClassifier | None = None

_DETECT_W, _DETECT_H = 320, 240


def _get_hog() -> cv2.HOGDescriptor:
    global _hog
    if _hog is None:
        _hog = cv2.HOGDescriptor()
        _hog.setSVMDetector(cv2.HOGDescriptor_getDefaultPeopleDetector())
    return _hog


def _get_face_cascade() -> cv2.CascadeClassifier:
    global _face_cascade
    if _face_cascade is None:
        _face_cascade = cv2.CascadeClassifier(
            cv2.data.haarcascades + "haarcascade_frontalface_default.xml"
        )
    return _face_cascade


# ── 纯检测函数 ────────────────────────────────────────────────────

def detect(frame_bytes: bytes, prev_person_count: int = -1) -> VisionFrame:
    """纯函数：分析一帧 JPEG，返回结构化检测结果，无副作用。"""
    arr = np.frombuffer(frame_bytes, dtype=np.uint8)
    img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    img = cv2.resize(img, (_DETECT_W, _DETECT_H))
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)

    rects, _ = _get_hog().detectMultiScale(
        img, winStride=(8, 8), padding=(4, 4), scale=1.05
    )
    person_count = len(rects)

    faces = _get_face_cascade().detectMultiScale(
        gray, scaleFactor=1.1, minNeighbors=5, minSize=(30, 30)
    )
    face_count = len(faces)

    if prev_person_count < 0:
        changed, change_type = False, "none"
    elif prev_person_count == 0 and person_count > 0:
        changed, change_type = True, "person_entered"
    elif prev_person_count > 0 and person_count == 0:
        changed, change_type = True, "person_left"
    elif prev_person_count != person_count:
        changed, change_type = True, "count_changed"
    else:
        changed, change_type = False, "none"

    return VisionFrame(
        ts=time.time(),
        persons=PersonInfo(detected=person_count > 0, count=person_count),
        faces=FaceInfo(detected=face_count > 0, count=face_count),
        changed=changed,
        change_type=change_type,
    )


# ── 后台检测循环 ──────────────────────────────────────────────────

OnFrameCallback = Callable[[VisionFrame], None]


class VisionLoop:
    """
    每隔 interval 秒从 frame_provider 取最新帧，在线程池中运行 OpenCV 检测，
    将结果广播给所有已注册的回调。

    与其他模块完全解耦：通过 frame_provider 依赖注入帧来源，
    通过 add_callback / remove_callback 注入消费者。
    """

    def __init__(self, interval: float = 1.0) -> None:
        self.interval = interval
        self.latest: VisionFrame | None = None
        self._callbacks: list[OnFrameCallback] = []
        self._task: asyncio.Task | None = None
        self._prev_person_count: int = -1

    def add_callback(self, cb: OnFrameCallback) -> None:
        self._callbacks.append(cb)

    def remove_callback(self, cb: OnFrameCallback) -> None:
        try:
            self._callbacks.remove(cb)
        except ValueError:
            pass

    def start(self, frame_provider: Callable[[], bytes | None]) -> None:
        if self._task and not self._task.done():
            return
        self._prev_person_count = -1
        self._task = asyncio.create_task(self._run(frame_provider))
        logger.info("[Vision] 检测循环已启动（间隔 %.1fs）", self.interval)

    def stop(self) -> None:
        if self._task:
            self._task.cancel()
            self._task = None
        self.latest = None
        self._prev_person_count = -1
        logger.info("[Vision] 检测循环已停止")

    async def _run(self, frame_provider: Callable[[], bytes | None]) -> None:
        loop = asyncio.get_event_loop()
        while True:
            await asyncio.sleep(self.interval)
            frame_bytes = frame_provider()
            if frame_bytes is None:
                continue
            try:
                result: VisionFrame = await loop.run_in_executor(
                    None, detect, frame_bytes, self._prev_person_count
                )
                self._prev_person_count = result["persons"]["count"]
                self.latest = result
                for cb in list(self._callbacks):
                    try:
                        cb(result)
                    except Exception as exc:
                        logger.warning("[Vision] 回调异常: %s", exc)
            except Exception as exc:
                logger.warning("[Vision] 检测失败: %s", exc)


vision_loop = VisionLoop()
