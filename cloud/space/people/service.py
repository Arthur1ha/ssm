"""空间人员库：手机录入照片，所有设备共享身份与关系元数据。"""

import asyncio
import base64
import io
import logging
import os
import time
import uuid

from PIL import Image, ImageOps, UnidentifiedImageError

from cloud.space.execution import require_persist
from .memory import KnownPerson, PeopleMemory
from .provider import (
    FaceImageRejected, FaceProvider, FaceProviderError, FaceProviderNotConfigured,
)
from .tencent import create_tencent_face_provider_from_env

logger = logging.getLogger(__name__)


def _photo_jpeg(image: bytes) -> bytes:
    if not image:
        raise FaceImageRejected("empty_image", "请选择照片")
    try:
        with Image.open(io.BytesIO(image)) as source:
            source.draft("RGB", (4000, 4000))
            source.thumbnail((4000, 4000))
            photo = ImageOps.exif_transpose(source).convert("RGB")
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError) as exc:
        raise FaceImageRejected("invalid_image", "照片格式无效") from exc
    if min(photo.size) < 64:
        raise FaceImageRejected("invalid_image_dimensions", "照片短边至少需要 64 像素")
    while True:
        output = io.BytesIO()
        photo.save(output, format="JPEG", quality=85, optimize=True)
        jpeg = output.getvalue()
        if len(base64.b64encode(jpeg)) <= 5_000_000:
            return jpeg
        width, height = photo.size
        if min(width, height) <= 64:
            raise FaceImageRejected("image_too_large", "照片处理后仍超过人脸服务的 5M 限制")
        photo.thumbnail((max(64, int(width * 0.8)), max(64, int(height * 0.8))))


class SpacePeopleService:
    def __init__(self, provider: FaceProvider | None, people: PeopleMemory, *, writes_enabled: bool = True) -> None:
        self.provider = provider
        self.people = people
        self.writes_enabled = writes_enabled
        self._write_lock = asyncio.Lock()

    def list_people(self) -> tuple[KnownPerson, ...]:
        return self.people.list()

    def _require_write(self) -> FaceProvider:
        require_persist("modify space people")
        if not self.writes_enabled or self.provider is None:
            raise FaceProviderNotConfigured("enrollment_disabled", "空间人员库暂不可写")
        return self.provider

    async def create(self, name: str, role: str, image: bytes) -> KnownPerson:
        provider = self._require_write()
        name, role = name.strip(), role.strip().lower()
        if not name or len(name) > 64 or any(ord(char) < 32 for char in name):
            raise ValueError("姓名必须是 1~64 个可显示字符")
        if role not in {"owner", "family", "friend", "stranger"}:
            raise ValueError("关系必须是主人、家人、朋友或陌生人")
        jpeg = _photo_jpeg(image)
        async with self._write_lock:
            if role == "owner" and self.people.get_owner() is not None:
                raise ValueError("主人已经存在")
            person_id = f"space_person_{uuid.uuid4().hex}"
            await provider.create_person(person_id, name, jpeg)
            now = time.time()
            person = KnownPerson(
                person_id=person_id, name=name, role=role, provider="tencent_iai",
                group_id=getattr(provider, "group_id", ""), face_count=1,
                created_ts=now, updated_ts=now,
            )
            try:
                return self.people.upsert(person)
            except Exception:
                logger.exception("[SpacePeople] 云端创建成功但本地元数据写入失败 person_id=%s", person_id)
                raise

    async def delete(self, person_id: str) -> None:
        provider = self._require_write()
        async with self._write_lock:
            if not person_id or self.people.get(person_id) is None:
                raise ValueError("必须指定已登记的 person_id")
            try:
                await provider.remove_from_group(person_id)
            except FaceProviderError as exc:
                if "NotExist" not in exc.code and "NotFound" not in exc.code:
                    raise
            self.people.remove(person_id)


def _default_service() -> SpacePeopleService:
    try:
        provider = create_tencent_face_provider_from_env()
    except FaceProviderNotConfigured:
        provider = None
    enabled = os.getenv("SPACE_FACE_ENROLLMENT_ENABLED", os.getenv("GO2_FACE_ENROLLMENT_ENABLED", "true"))
    return SpacePeopleService(provider, PeopleMemory(), writes_enabled=enabled.lower() in {"1", "true", "yes", "on"})


space_people = _default_service()
