import asyncio
import logging
import time
from collections import OrderedDict
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
    boxes: list[tuple[int, int, int, int]]


class VisionFrame(TypedDict):
    frame_id: int
    ts: float
    persons: PersonInfo
    faces: FaceInfo
    changed: bool
    change_type: str  # "person_entered" | "person_left" | "count_changed" | "none"


# ── 检测器惰性初始化 ──────────────────────────────────────────────

_hog: cv2.HOGDescriptor | None = None
_face_cascade: cv2.CascadeClassifier | None = None

_DETECT_W, _DETECT_H = 320, 240


class VisionFaceRejected(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


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

def detect(frame_bytes: bytes, prev_person_count: int = -1, frame_id: int = 0) -> VisionFrame:
    """纯函数：分析一帧 JPEG，返回结构化检测结果，无副作用。"""
    arr = np.frombuffer(frame_bytes, dtype=np.uint8)
    img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    if img is None:
        raise VisionFaceRejected("invalid_jpeg", "视频帧不是有效 JPEG")
    original_h, original_w = img.shape[:2]
    detect_img = cv2.resize(img, (_DETECT_W, _DETECT_H))
    gray = cv2.cvtColor(detect_img, cv2.COLOR_BGR2GRAY)

    rects, _ = _get_hog().detectMultiScale(
        detect_img, winStride=(8, 8), padding=(4, 4), scale=1.05
    )
    person_count = len(rects)

    faces = _get_face_cascade().detectMultiScale(
        gray, scaleFactor=1.1, minNeighbors=5, minSize=(30, 30)
    )
    face_count = len(faces)
    scale_x = original_w / _DETECT_W
    scale_y = original_h / _DETECT_H
    face_boxes = [
        (
            max(0, round(int(x) * scale_x)),
            max(0, round(int(y) * scale_y)),
            min(original_w, round(int(w) * scale_x)),
            min(original_h, round(int(h) * scale_y)),
        )
        for x, y, w, h in faces
    ]

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
        frame_id=frame_id,
        ts=time.time(),
        persons=PersonInfo(detected=person_count > 0, count=person_count),
        faces=FaceInfo(detected=face_count > 0, count=face_count, boxes=face_boxes),
        changed=changed,
        change_type=change_type,
    )


def crop_face_jpeg(
    frame_bytes: bytes,
    box: tuple[int, int, int, int],
    padding: float = 0.2,
) -> bytes:
    """按原图坐标裁剪人脸；padding 按四边各增加脸宽/高的比例。"""
    arr = np.frombuffer(frame_bytes, dtype=np.uint8)
    img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    if img is None:
        raise VisionFaceRejected("invalid_jpeg", "视频帧不是有效 JPEG")
    height, width = img.shape[:2]
    x, y, w, h = box
    if w <= 0 or h <= 0:
        raise VisionFaceRejected("invalid_face_box", "人脸框无效")
    pad_x, pad_y = round(w * padding), round(h * padding)
    x1, y1 = max(0, x - pad_x), max(0, y - pad_y)
    x2, y2 = min(width, x + w + pad_x), min(height, y + h + pad_y)
    crop = img[y1:y2, x1:x2]
    if crop.size == 0:
        raise VisionFaceRejected("invalid_face_box", "人脸框超出画面")
    ok, encoded = cv2.imencode(".jpg", crop, [cv2.IMWRITE_JPEG_QUALITY, 92])
    if not ok:
        raise VisionFaceRejected("jpeg_encode_failed", "人脸裁剪编码失败")
    return encoded.tobytes()


def select_enrollment_face(frame: VisionFrame, frame_bytes: bytes) -> bytes:
    """选择唯一且足够大的人脸并返回裁剪 JPEG。"""
    boxes = frame["faces"].get("boxes", [])
    if len(boxes) == 0:
        raise VisionFaceRejected("no_face", "画面中没有检测到人脸")
    if len(boxes) != 1:
        raise VisionFaceRejected("multiple_faces", "登记时画面中必须恰好有一张人脸")
    if min(boxes[0][2], boxes[0][3]) < 80:
        raise VisionFaceRejected("face_too_small", "人脸过小，请靠近镜头后重试")
    return crop_face_jpeg(frame_bytes, boxes[0])


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
        self._next_frame_id = 1
        self._frames: OrderedDict[int, bytes] = OrderedDict()

    def add_callback(self, cb: OnFrameCallback) -> None:
        if cb not in self._callbacks:
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
        self._frames.clear()
        logger.info("[Vision] 检测循环已停止")

    def get_frame(self, frame_id: int | None = None) -> bytes | None:
        if frame_id is None:
            frame_id = self.latest["frame_id"] if self.latest else None
        return self._frames.get(frame_id) if frame_id is not None else None

    def latest_frame_pair(self) -> tuple[VisionFrame | None, bytes | None]:
        frame = self.latest
        return frame, self.get_frame(frame["frame_id"] if frame else None)

    async def _run(self, frame_provider: Callable[[], bytes | None]) -> None:
        loop = asyncio.get_event_loop()
        while True:
            await asyncio.sleep(self.interval)
            frame_bytes = frame_provider()
            if frame_bytes is None:
                continue
            try:
                frame_id = self._next_frame_id
                self._next_frame_id += 1
                result: VisionFrame = await loop.run_in_executor(
                    None, detect, frame_bytes, self._prev_person_count, frame_id
                )
                self._frames[frame_id] = frame_bytes
                while len(self._frames) > 2:
                    self._frames.popitem(last=False)
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
