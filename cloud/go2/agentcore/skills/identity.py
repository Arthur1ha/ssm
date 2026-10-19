"""Go2 身份运行时 skill：视觉帧、云端 provider 与本地关系记忆的唯一编排层。"""

import asyncio
import logging
import os
import re
import time
import uuid
from dataclasses import dataclass
from typing import Callable

from cloud.go2.agentcore.identity.provider import (
    DuplicateFaceCandidate,
    FaceProvider,
    FaceProviderError,
    FaceProviderNotConfigured,
)
from cloud.go2.agentcore.memory.people import KnownPerson, PeopleMemory
from cloud.go2.agentcore.skills.vision import VisionFaceRejected, VisionFrame, select_enrollment_face

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
        enrollment_enabled: bool = False,
        accept_score: float = 90.0,
        candidate_score: float = 80.0,
        recheck_s: float = 30.0,
        absent_s: float = 3.0,
    ) -> None:
        self.provider = provider
        self.people = people
        self._frame_getter = frame_getter
        self.recognition_enabled = recognition_enabled and provider is not None
        self.enrollment_enabled = enrollment_enabled and provider is not None
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

    def _validate_enrollment(self, name: str, role: str) -> tuple[str, str]:
        if not self.enrollment_enabled or self.provider is None:
            raise FaceProviderNotConfigured("enrollment_disabled", "人员登记功能未开启")
        name, role = name.strip(), role.strip().lower()
        if not name or len(name) > 64 or any(ord(c) < 32 for c in name):
            raise ValueError("姓名必须是 1~64 个可显示字符")
        if not re.fullmatch(r"[a-z0-9_-]{1,32}", role):
            raise ValueError("关系只允许字母、数字、下划线和连字符")
        if role == "owner" and self.people.get_owner() is not None:
            raise ValueError("主人已经存在；如需补录，请使用增加人脸功能")
        return name, role

    async def remember_person(self, name: str, role: str) -> IdentitySnapshot:
        try:
            name, role = self._validate_enrollment(name, role)
            crop = self._get_face_crop()
        except (ValueError, VisionFaceRejected, FaceProviderError) as exc:
            return self._set_current(self._snapshot("rejected", error_code=getattr(exc, "code", "invalid_person")))

        person_id = "go2_owner" if role == "owner" else f"go2_person_{uuid.uuid4().hex}"
        self._set_current(self._snapshot("enrolling", person_id=person_id, name=name, role=role))
        try:
            await self.provider.create_person(person_id, name, crop)  # type: ignore[union-attr]
        except DuplicateFaceCandidate as exc:
            return self._set_current(self._snapshot(
                "rejected", person_id=exc.person_id, error_code=exc.code,
            ))
        except FaceProviderError as exc:
            return self._set_current(self._snapshot("error", error_code=exc.code))

        now = time.time()
        person = KnownPerson(
            person_id=person_id, name=name, role=role, provider="tencent_iai",
            group_id=getattr(self.provider, "group_id", ""), face_count=1,
            created_ts=now, updated_ts=now,
        )
        try:
            self.people.upsert(person)
        except Exception:
            logger.exception("[Identity] 云端已创建但本地元数据写入失败 person_id=%s", person_id)
            return self._set_current(self._snapshot(
                "error", person_id=person_id, name=name, role=role,
                error_code="local_persist_failed",
            ))
        return self._set_current(self._recognized(person, 100.0))

    async def add_person_face(self, person_id: str) -> IdentitySnapshot:
        if not self.enrollment_enabled or self.provider is None:
            return self._set_current(self._snapshot("disabled", error_code="enrollment_disabled"))
        person = self.people.get(person_id)
        if person is None:
            return self._set_current(self._snapshot("rejected", error_code="person_not_found"))
        if person.face_count >= 5:
            return self._set_current(self._snapshot("rejected", person_id=person_id, error_code="face_limit"))
        try:
            crop = self._get_face_crop()
            await self.provider.add_face(person_id, crop)
            updated = self.people.increment_face_count(person_id)
        except (VisionFaceRejected, FaceProviderError, ValueError) as exc:
            return self._set_current(self._snapshot("error", person_id=person_id, error_code=getattr(exc, "code", "add_face_failed")))
        return self._set_current(self._recognized(updated, self._current.score or 100.0))

    async def forget_person(self, person_id: str) -> None:
        if not self.enrollment_enabled or self.provider is None:
            raise FaceProviderNotConfigured("enrollment_disabled", "人员删除功能未开启")
        if not person_id or self.people.get(person_id) is None:
            raise ValueError("必须指定已登记的 person_id")
        try:
            await self.provider.remove_from_group(person_id)
        except FaceProviderError as exc:
            if "NotExist" not in exc.code and "NotFound" not in exc.code:
                raise
        self.people.remove(person_id)
        if self._current.person_id == person_id:
            self.clear_transient()

    def list_people(self) -> tuple[KnownPerson, ...]:
        return self.people.list()

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
            self.enrollment_enabled = False
            self._set_current(self._snapshot("disabled", error_code=exc.code))
            return False


def _default_frame_getter() -> tuple[VisionFrame | None, bytes | None]:
    from cloud.go2.agentcore.skills.vision import vision_loop
    return vision_loop.latest_frame_pair()


def _build_default_identity_skill() -> IdentitySkill:
    provider = None
    try:
        from cloud.go2.agentcore.identity.tencent import create_tencent_face_provider_from_env
        provider = create_tencent_face_provider_from_env()
    except FaceProviderNotConfigured:
        pass
    return IdentitySkill(
        provider,
        PeopleMemory(),
        _default_frame_getter,
        recognition_enabled=_env_bool("GO2_FACE_RECOGNITION_ENABLED", True),
        enrollment_enabled=_env_bool("GO2_FACE_ENROLLMENT_ENABLED", False),
        accept_score=float(os.getenv("GO2_FACE_ACCEPT_SCORE", "90")),
        candidate_score=float(os.getenv("GO2_FACE_CANDIDATE_SCORE", "80")),
        recheck_s=float(os.getenv("GO2_FACE_RECHECK_S", "30")),
    )


identity_skill = _build_default_identity_skill()
