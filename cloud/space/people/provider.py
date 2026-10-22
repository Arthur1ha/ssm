"""与云厂商无关的人脸服务契约。"""

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class FaceCandidate:
    person_id: str
    person_name: str
    score: float


@dataclass(frozen=True)
class FaceSearchResult:
    candidates: tuple[FaceCandidate, ...]
    request_id: str = ""


@dataclass(frozen=True)
class GroupInfo:
    group_id: str
    group_name: str
    face_model_version: str
    request_id: str = ""


@dataclass(frozen=True)
class CreatedPerson:
    person_id: str
    face_id: str
    request_id: str = ""


@dataclass(frozen=True)
class RemotePerson:
    person_id: str
    person_name: str
    face_count: int


class FaceProviderError(RuntimeError):
    """可安全返回给上层的 provider 错误，不包含请求体或凭据。"""

    def __init__(self, code: str, message: str, request_id: str = "") -> None:
        super().__init__(message)
        self.code = code
        self.request_id = request_id


class FaceProviderNotConfigured(FaceProviderError):
    pass


class FaceImageRejected(FaceProviderError):
    pass


class FaceProviderUnavailable(FaceProviderError):
    pass


class DuplicateFaceCandidate(FaceProviderError):
    def __init__(self, person_id: str, request_id: str = "") -> None:
        super().__init__("duplicate_face", "该人脸疑似已在人员库中", request_id)
        self.person_id = person_id


class FaceProvider(Protocol):
    async def check_group(self) -> GroupInfo: ...
    async def create_person(self, person_id: str, name: str, jpeg: bytes) -> CreatedPerson: ...
    async def search_persons(self, jpeg: bytes) -> FaceSearchResult: ...
    async def list_people(self) -> tuple[RemotePerson, ...]: ...
    async def remove_from_group(self, person_id: str) -> None: ...
