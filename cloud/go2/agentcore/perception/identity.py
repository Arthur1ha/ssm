"""Go2 身份运行时 skill：视觉帧、云端 provider 与本地关系记忆的唯一编排层。"""

import asyncio
import logging
import os
import time
from dataclasses import dataclass
from typing import Callable

from cloud.space.people.provider import (
    FaceProvider,
    FaceProviderError,
)
from cloud.space.people.memory import KnownPerson, PeopleMemory
from cloud.go2.agentcore.perception.vision import VisionFaceRejected, VisionFrame, select_enrollment_face
from cloud.space.people.service import space_people

logger = logging.getLogger(__name__)


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
        accept_score: float = 90.0,
        candidate_score: float = 80.0,
        recheck_s: float = 30.0,
        absent_s: float = 3.0,
    ) -> None:
        self.provider = provider
        self.people = people
        self._frame_getter = frame_getter
        self.recognition_enabled = recognition_enabled and provider is not None
        self.accept_score = accept_score
        self.candidate_score = candidate_score
        self.recheck_s = recheck_s
        self.absent_s = absent_s
        initial = "idle" if self.recognition_enabled else "disabled"
        self._current = IdentitySnapshot(status=initial, ts=time.time())
        self._callbacks: list[IdentityCallback] = []
        self._search_lock = asyncio.Lock()
        self._auto_task: asyncio.Task | None = None
        self._last_face_seen = 0.0
        self._last_search_ts = 0.0
        self._previous_face_count = 0
        self._candidate_id = ""
        self._candidate_count = 0

    @property
    def enabled(self) -> bool:
        return self.recognition_enabled

    @property
    def current(self) -> IdentitySnapshot:
        return self._current

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
            event = IdentityEvent(previous, snapshot)
            for callback in tuple(self._callbacks):
                try:
                    callback(event)
                except Exception:
                    logger.exception("[Identity] 回调失败")
        return snapshot

    def _snapshot(self, status: str, **kwargs) -> IdentitySnapshot:
        return IdentitySnapshot(status=status, ts=time.time(), **kwargs)

    def _get_face_crop(self) -> bytes:
        frame, jpeg = self._frame_getter()
        if frame is None or jpeg is None:
            raise VisionFaceRejected("no_frame", "暂无可用视频帧")
        return select_enrollment_face(frame, jpeg)

    def on_vision_frame(self, frame: VisionFrame) -> None:
        """同步轻量入口；云调用只在后台 task 中发生。"""
        now = time.time()
        face_count = int(frame["faces"]["count"])
        if face_count > 0:
            self._last_face_seen = now
        elif self._previous_face_count > 0:
            self._last_face_seen = now
        elif self._current.status in {"recognized", "candidate", "unknown", "error"}:
            if now - self._last_face_seen >= self.absent_s:
                self.clear_transient()

        should_search = (
            self.recognition_enabled
            and face_count == 1
            and (self._previous_face_count == 0 or now - self._last_search_ts >= self.recheck_s)
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
            self._set_current(self._snapshot("searching"))
            self._last_search_ts = time.time()
            try:
                crop = self._get_face_crop()
                result = await self.provider.search_persons(crop)
            except (VisionFaceRejected, FaceProviderError) as exc:
                code = getattr(exc, "code", "recognition_failed")
                return self._set_current(self._snapshot("error", error_code=code))

            candidate = result.candidates[0] if result.candidates else None
            if candidate is None or candidate.score < self.candidate_score:
                self._candidate_id = ""
                self._candidate_count = 0
                return self._set_current(self._snapshot("unknown"))

            person = self.people.get(candidate.person_id)
            if person is None:
                self._candidate_id = ""
                self._candidate_count = 0
                return self._set_current(self._snapshot("unknown", score=candidate.score))

            if candidate.score >= self.accept_score:
                self._candidate_id = ""
                self._candidate_count = 0
                return self._set_current(self._recognized(person, candidate.score))

            if self._candidate_id == candidate.person_id:
                self._candidate_count += 1
            else:
                self._candidate_id = candidate.person_id
                self._candidate_count = 1
            if self._candidate_count >= 2:
                self._candidate_id = ""
                self._candidate_count = 0
                return self._set_current(self._recognized(person, candidate.score))
            return self._set_current(self._snapshot(
                "candidate", person_id=person.person_id, name=person.name,
                role=person.role, score=candidate.score,
            ))

    def _recognized(self, person: KnownPerson, score: float) -> IdentitySnapshot:
        return self._snapshot(
            "recognized", person_id=person.person_id, name=person.name,
            role=person.role, score=score,
        )

    def clear_transient(self) -> None:
        if self._auto_task and not self._auto_task.done():
            self._auto_task.cancel()
        self._candidate_id = ""
        self._candidate_count = 0
        self._previous_face_count = 0
        status = "idle" if self.recognition_enabled else "disabled"
        self._set_current(self._snapshot(status))

    async def check_provider(self) -> bool:
        """验证人员库存在且为 3.0；失败只禁用身份，不影响其他 Go2 能力。"""
        if self.provider is None or not self.recognition_enabled:
            return False
        try:
            await self.provider.check_group()
            return True
        except FaceProviderError as exc:
            logger.warning("[Identity] 人员库健康检查失败 code=%s", exc.code)
            self.recognition_enabled = False
            self._set_current(self._snapshot("disabled", error_code=exc.code))
            return False


def _default_frame_getter() -> tuple[VisionFrame | None, bytes | None]:
    from cloud.go2.agentcore.perception.vision import vision_loop
    return vision_loop.latest_frame_pair()


def _build_default_identity_skill() -> IdentitySkill:
    return IdentitySkill(
        space_people.provider,
        space_people.people,
        _default_frame_getter,
        recognition_enabled=_env_bool("GO2_FACE_RECOGNITION_ENABLED", True),
        accept_score=float(os.getenv("GO2_FACE_ACCEPT_SCORE", "90")),
        candidate_score=float(os.getenv("GO2_FACE_CANDIDATE_SCORE", "80")),
        recheck_s=float(os.getenv("GO2_FACE_RECHECK_S", "30")),
    )


identity_skill = _build_default_identity_skill()
