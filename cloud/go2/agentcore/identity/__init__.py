"""Go2 人脸身份 provider 契约与实现。"""

from .provider import (
    AddedFace,
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
    "AddedFace", "CreatedPerson", "DuplicateFaceCandidate", "FaceCandidate",
    "FaceImageRejected", "FaceProvider", "FaceProviderNotConfigured",
    "FaceProviderUnavailable", "FaceSearchResult", "GroupInfo", "RemotePerson",
]
