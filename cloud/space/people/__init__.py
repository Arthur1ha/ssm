"""空间共享人员库与人脸服务。"""

from .memory import KnownPerson, PeopleMemory

from .provider import (
    CreatedPerson,
    DuplicateFaceCandidate,
    FaceCandidate,
    FaceImageRejected,
    FaceProvider,
    FaceProviderNotConfigured,
    FaceProviderUnavailable,
    FaceSearchResult,
    GroupInfo,
    RemotePerson,
)

__all__ = [
    "KnownPerson", "PeopleMemory", "CreatedPerson", "DuplicateFaceCandidate", "FaceCandidate",
    "FaceImageRejected", "FaceProvider", "FaceProviderNotConfigured",
    "FaceProviderUnavailable", "FaceSearchResult", "GroupInfo", "RemotePerson",
]
