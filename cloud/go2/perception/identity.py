"""Go2 身份运行时 skill：视觉帧、云端 provider 与本地关系记忆的唯一编排层。"""

import asyncio
import logging
import os
import time
from dataclasses import asdict, dataclass
from typing import Callable

from cloud.space.people.provider import (
    FACE_MATCH_THRESHOLD,
    FaceImageRejected,
    FaceProvider,
    FaceProviderError,
)
from cloud.space.people.memory import KnownPerson, PeopleMemory
from cloud.go2.perception.vision import (
    FRAME_MAX_AGE_S, VisionFaceRejected, VisionFrame, frame_is_fresh, select_enrollment_face,
)
from cloud.space.people.service import space_people

logger = logging.getLogger(__name__)
PROVIDER_RETRY_S = 5.0


def _same_face(previous: dict, current: dict) -> bool:
    """Reject a changed camera session or discontinuous single-face location.

    Geometry is continuity evidence only. Identity still expires after five
    seconds and must be verified again by the provider.
    """
    if previous.get("camera_session_id") != current.get("camera_session_id"):
        return False
    before = (previous.get("faces") or {}).get("boxes", [])
    after = (current.get("faces") or {}).get("boxes", [])
    if len(before) != 1 or len(after) != 1:
        return False
    x, y, w, h = before[0]
    xx, yy, ww, hh = after[0]
    overlap = max(0, min(x+w, xx+ww)-max(x, xx)) * max(0, min(y+h, yy+hh)-max(y, yy))
    union = w*h + ww*hh - overlap
    return union > 0 and overlap / union >= 0.2


def _env_bool(name: str, default: bool = False) -> bool:
    return os.getenv(name, "true" if default else "false").strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class IdentitySnapshot:
    status: str
    person_id: str = ""
    name: str = ""
    role: str = ""
    score: float = 0.0
    ts: float = 0.0
    error_code: str = ""
    frame_id: int | None = None


@dataclass(frozen=True)
class IdentityEvent:
    previous: IdentitySnapshot
    current: IdentitySnapshot


IdentityCallback = Callable[[IdentityEvent], None]
FrameGetter = Callable[[], tuple[VisionFrame | None, bytes | None]]


class IdentitySkill:
    def __init__(
        self,
        provider: FaceProvider | None,
        people: PeopleMemory,
        frame_getter: FrameGetter,
        *,
        recognition_enabled: bool = True,
        recheck_s: float = 5.0,
    ) -> None:
        self.provider = provider
        self.people = people
        self._frame_getter = frame_getter
        self.recognition_enabled = recognition_enabled and provider is not None
        self.recheck_s = min(FRAME_MAX_AGE_S, max(1.0, recheck_s))
        self._provider_ready = True
        self._health_retry_at = 0.0
        self._health_lock = asyncio.Lock()
        initial = "idle" if self.recognition_enabled else "disabled"
        self._current = IdentitySnapshot(status=initial, ts=time.time())
        self._callbacks: list[IdentityCallback] = []
        self._search_lock = asyncio.Lock()
        self._auto_task: asyncio.Task | None = None
        self._last_search_ts = 0.0
        self._previous_face_count = 0
        self._face_generation = 0
        self._last_frame: VisionFrame | None = None
        self._identity_frame: VisionFrame | None = None

    @property
    def enabled(self) -> bool:
        return self.recognition_enabled

    @property
    def available(self) -> bool:
        return self.enabled and self._provider_ready

    @property
    def current(self) -> IdentitySnapshot:
        if self._current.status in {"recognized", "unknown"}:
            frame, _ = self._frame_getter()
            if (not frame_is_fresh(frame) or not frame_is_fresh(asdict(self._current))
                    or frame["faces"]["count"] != 1
                    or self._identity_frame is not None and not _same_face(self._identity_frame, frame)):
                self._set_current(self._snapshot("idle", error_code="identity_expired"))
        return self._current

    def context(self) -> dict:
        """A live projection; historical identity never supplies current facts."""
        return asdict(self.current)

    def add_callback(self, cb: IdentityCallback) -> None:
        if cb not in self._callbacks:
            self._callbacks.append(cb)

    def remove_callback(self, cb: IdentityCallback) -> None:
        try:
            self._callbacks.remove(cb)
        except ValueError:
            pass

    def _set_current(self, snapshot: IdentitySnapshot) -> IdentitySnapshot:
        previous = self._current
        self._current = snapshot
        if (previous.status, previous.person_id, previous.error_code) != (
            snapshot.status, snapshot.person_id, snapshot.error_code
        ):
            logger.info("[Identity] status=%s error=%s frame_id=%s score=%.1f",
                        snapshot.status, snapshot.error_code, snapshot.frame_id, snapshot.score)
            event = IdentityEvent(previous, snapshot)
            for callback in tuple(self._callbacks):
                try:
                    callback(event)
                except Exception:
                    logger.exception("[Identity] 回调失败")
        return snapshot

    def _snapshot(self, status: str, **kwargs) -> IdentitySnapshot:
        kwargs.setdefault("ts", time.time())
        return IdentitySnapshot(status=status, **kwargs)

    def _get_face_crop(self) -> bytes:
        frame, jpeg = self._frame_getter()
        if frame is None or jpeg is None:
            raise VisionFaceRejected("no_frame", "暂无可用视频帧")
        if not frame_is_fresh(frame):
            raise VisionFaceRejected("stale_frame", "视频帧已过期，请等待新画面")
        return select_enrollment_face(frame, jpeg)

    def on_vision_frame(self, frame: VisionFrame) -> None:
        """同步轻量入口；云调用只在后台 task 中发生。"""
        now = time.time()
        if not frame_is_fresh(frame, now=now):
            self.clear_transient()
            return
        if self._last_frame is not None and frame["ts"] <= self._last_frame["ts"]:
            return
        face_count = int(frame["faces"]["count"])
        changed = (face_count != self._previous_face_count
                   or self._last_frame is not None and face_count == 1
                   and not _same_face(self._last_frame, frame)
                   or face_count != 1 and self._current.status in {"recognized", "unknown", "searching"})
        if changed:
            self._face_generation += 1
            self._last_search_ts = 0.0
            self._identity_frame = None
            if self.available:
                reason = "face_absent" if face_count == 0 else "multiple_faces" if face_count > 1 else "face_changed"
                self._set_current(self._snapshot("idle", error_code=reason))
        self._last_frame = frame

        should_search = (
            self.recognition_enabled
            and face_count == 1
            and now >= self._health_retry_at
            and (changed or now - self._last_search_ts >= self.recheck_s)
            and not self._search_lock.locked()
            and (self._auto_task is None or self._auto_task.done())
        )
        self._previous_face_count = face_count
        if not should_search:
            return
        try:
            self._auto_task = asyncio.get_running_loop().create_task(self.recognize_now())
            self._auto_task.add_done_callback(self._consume_task_result)
        except RuntimeError:
            return

    @staticmethod
    def _consume_task_result(task: asyncio.Task) -> None:
        if task.cancelled():
            return
        try:
            task.result()
        except Exception:
            logger.exception("[Identity] 自动识别任务异常")

    async def recognize_now(self) -> IdentitySnapshot:
        if not self.recognition_enabled or self.provider is None:
            return self._set_current(self._snapshot("disabled", error_code="not_configured"))
        if self._search_lock.locked():
            return self._snapshot("searching", error_code="busy")
        async with self._search_lock:
            if not self._provider_ready and not await self.check_provider(force=False):
                return self._current
            face_generation = self._face_generation
            self._set_current(self._snapshot("searching"))
            frame, _ = self._frame_getter()
            try:
                crop = self._get_face_crop()
                self._last_search_ts = time.time()
                result = await self.provider.search_persons(crop)
            except (VisionFaceRejected, FaceProviderError) as exc:
                if face_generation != self._face_generation:
                    return self._current
                if isinstance(exc, FaceProviderError) and not isinstance(exc, FaceImageRejected):
                    self._provider_ready = False
                    self._health_retry_at = time.time() + PROVIDER_RETRY_S
                code = getattr(exc, "code", "recognition_failed")
                return self._set_current(self._snapshot("error", error_code=code))

            latest, _ = self._frame_getter()
            if (face_generation != self._face_generation or not frame_is_fresh(frame)
                    or not frame_is_fresh(latest) or latest["faces"]["count"] != 1
                    or not _same_face(frame, latest)):
                # The result belongs to a face that left or a different group
                # of faces. A later fresh frame must initiate a new search.
                self._last_search_ts = 0.0
                return self._set_current(self._snapshot("idle"))

            self._identity_frame = frame
            candidate = result.candidates[0] if result.candidates else None
            person = (self.people.get(candidate.person_id)
                      if candidate is not None and candidate.score >= FACE_MATCH_THRESHOLD else None)
            if person is None:
                return self._set_current(self._snapshot(
                    "unknown", name="陌生人", role="stranger",
                    score=candidate.score if candidate is not None else 0.0,
                    ts=frame["ts"], frame_id=frame.get("frame_id"),
                ))
            return self._set_current(self._recognized(person, candidate.score, frame))

    def _recognized(self, person: KnownPerson, score: float, frame: dict | None = None) -> IdentitySnapshot:
        return self._snapshot(
            "recognized", person_id=person.person_id, name=person.name,
            role=person.role, score=score,
            ts=frame["ts"] if frame is not None else time.time(),
            frame_id=frame.get("frame_id") if frame is not None else None,
        )

    def clear_transient(self) -> None:
        self._face_generation += 1
        if self._auto_task and not self._auto_task.done():
            self._auto_task.cancel()
        self._previous_face_count = 0
        self._last_search_ts = 0.0
        self._last_frame = self._identity_frame = None
        status = "idle" if self.recognition_enabled else "disabled"
        self._set_current(self._snapshot(status))

    async def check_provider(self, *, force: bool = True) -> bool:
        """Keep user configuration intact; transient failures have a bounded retry."""
        if self.provider is None or not self.recognition_enabled:
            return False
        async with self._health_lock:
            if not force and time.time() < self._health_retry_at:
                return False
            try:
                await self.provider.check_group()
                self._provider_ready = True
                self._health_retry_at = 0.0
                return True
            except FaceProviderError as exc:
                logger.warning("[Identity] 人员库健康检查失败 code=%s", exc.code)
                self._provider_ready = False
                self._health_retry_at = time.time() + PROVIDER_RETRY_S
                self._set_current(self._snapshot("error", error_code=exc.code))
                return False


def _default_frame_getter() -> tuple[VisionFrame | None, bytes | None]:
    from cloud.go2.perception.vision import vision_loop
    return vision_loop.latest_frame_pair()


def _build_default_identity_skill() -> IdentitySkill:
    return IdentitySkill(
        space_people.provider,
        space_people.people,
        _default_frame_getter,
        recognition_enabled=_env_bool("GO2_FACE_RECOGNITION_ENABLED", True),
        recheck_s=float(os.getenv("GO2_FACE_RECHECK_S", "5")),
    )


identity_skill = _build_default_identity_skill()
