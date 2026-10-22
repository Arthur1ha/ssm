"""腾讯云 IAI 2020-03-03 provider。"""

import asyncio
import base64
import logging
import os
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from .provider import (
    CreatedPerson,
    DuplicateFaceCandidate,
    FaceCandidate,
    FaceImageRejected,
    FaceProviderNotConfigured,
    FaceProviderUnavailable,
    FaceSearchResult,
    GroupInfo,
    RemotePerson,
)

logger = logging.getLogger(__name__)

_IMAGE_ERROR_MARKERS = (
    "NoFace", "FaceSize", "FaceQuality", "Image", "FaceNotComplete",
    "FaceIsCovered", "InvalidParameterValue.Face",
)


class TencentFaceProvider:
    def __init__(
        self,
        client: Any,
        group_id: str,
        *,
        timeout: float = 5.0,
        face_match_threshold: float = 80.0,
    ) -> None:
        if not group_id:
            raise FaceProviderNotConfigured("missing_group_id", "未配置腾讯云人员库")
        self._client = client
        self.group_id = group_id
        self.timeout = timeout
        self.face_match_threshold = face_match_threshold

    async def _call(self, method: Callable, request: Any) -> Any:
        # 每次调用使用短生命周期私有池。Python 3.13.12 + requests 环境中，
        # asyncio 默认池在首次任务后可能不再唤醒；身份请求本就单飞，单次池
        # 可避免任务堆积，也不会干扰 VisionLoop 使用的默认执行器。
        executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="tencent-face")
        try:
            loop = asyncio.get_running_loop()
            return await asyncio.wait_for(
                loop.run_in_executor(executor, method, request),
                timeout=self.timeout,
            )
        except asyncio.TimeoutError as exc:
            raise FaceProviderUnavailable("timeout", "人脸服务请求超时") from exc
        except Exception as exc:
            code = str(getattr(exc, "code", "provider_error") or "provider_error")
            request_id = str(getattr(exc, "requestId", "") or "")
            if any(marker in code for marker in _IMAGE_ERROR_MARKERS):
                raise FaceImageRejected(code, "当前画面不适合做人脸处理", request_id) from exc
            logger.warning("[Identity/Tencent] code=%s request_id=%s", code, request_id)
            raise FaceProviderUnavailable(code, "腾讯云人脸服务暂时不可用", request_id) from exc
        finally:
            executor.shutdown(wait=False, cancel_futures=True)

    @staticmethod
    def _image(jpeg: bytes) -> str:
        if not jpeg:
            raise FaceImageRejected("empty_image", "没有可用的人脸图像")
        return base64.b64encode(jpeg).decode("ascii")

    async def check_group(self) -> GroupInfo:
        from tencentcloud.iai.v20200303 import models

        req = models.GetGroupInfoRequest()
        req.GroupId = self.group_id
        resp = await self._call(self._client.GetGroupInfo, req)
        version = str(resp.FaceModelVersion or "")
        if version != "3.0":
            raise FaceProviderUnavailable(
                "unsupported_face_model", "人员库必须使用 FaceModelVersion 3.0",
                str(resp.RequestId or ""),
            )
        return GroupInfo(
            group_id=str(resp.GroupId or self.group_id),
            group_name=str(resp.GroupName or ""),
            face_model_version=version,
            request_id=str(resp.RequestId or ""),
        )

    async def create_person(self, person_id: str, name: str, jpeg: bytes) -> CreatedPerson:
        from tencentcloud.iai.v20200303 import models

        req = models.CreatePersonRequest()
        req.GroupId = self.group_id
        req.PersonId = person_id
        req.PersonName = name
        req.Image = self._image(jpeg)
        req.QualityControl = 3
        req.UniquePersonControl = 3
        resp = await self._call(self._client.CreatePerson, req)
        similar = str(resp.SimilarPersonId or "")
        if similar:
            raise DuplicateFaceCandidate(similar, str(resp.RequestId or ""))
        return CreatedPerson(person_id, str(resp.FaceId or ""), str(resp.RequestId or ""))

    async def search_persons(self, jpeg: bytes) -> FaceSearchResult:
        from tencentcloud.iai.v20200303 import models

        req = models.SearchPersonsRequest()
        req.GroupIds = [self.group_id]
        req.Image = self._image(jpeg)
        req.MaxFaceNum = 1
        req.MaxPersonNum = 3
        req.MinFaceSize = 80
        req.QualityControl = 2
        req.FaceMatchThreshold = self.face_match_threshold
        req.NeedPersonInfo = 1
        resp = await self._call(self._client.SearchPersons, req)
        candidates: list[FaceCandidate] = []
        for result in resp.Results or ():
            if getattr(result, "RetCode", 0) not in (None, 0):
                continue
            for item in getattr(result, "Candidates", None) or ():
                person_id = str(getattr(item, "PersonId", "") or "")
                score = getattr(item, "Score", None)
                if not person_id or not isinstance(score, (int, float)):
                    continue
                candidates.append(FaceCandidate(
                    person_id=person_id,
                    person_name=str(getattr(item, "PersonName", "") or ""),
                    score=float(score),
                ))
        candidates.sort(key=lambda item: item.score, reverse=True)
        return FaceSearchResult(tuple(candidates), str(resp.RequestId or ""))

    async def list_people(self) -> tuple[RemotePerson, ...]:
        from tencentcloud.iai.v20200303 import models

        people: list[RemotePerson] = []
        offset = 0
        while True:
            req = models.GetPersonListRequest()
            req.GroupId = self.group_id
            req.Offset = offset
            req.Limit = 1000
            resp = await self._call(self._client.GetPersonList, req)
            batch = tuple(resp.PersonInfos or ())
            for item in batch:
                people.append(RemotePerson(
                    person_id=str(item.PersonId or ""),
                    person_name=str(item.PersonName or ""),
                    face_count=len(item.FaceIds or ()),
                ))
            offset += len(batch)
            if not batch or offset >= int(resp.PersonNum or 0):
                break
        return tuple(people)

    async def remove_from_group(self, person_id: str) -> None:
        from tencentcloud.iai.v20200303 import models

        req = models.DeletePersonFromGroupRequest()
        req.GroupId = self.group_id
        req.PersonId = person_id
        await self._call(self._client.DeletePersonFromGroup, req)


def create_tencent_face_provider_from_env() -> TencentFaceProvider:
    secret_id = os.getenv("TENCENTCLOUD_SECRET_ID", "").strip()
    secret_key = os.getenv("TENCENTCLOUD_SECRET_KEY", "").strip()
    group_id = os.getenv("TENCENT_FACE_GROUP_ID", "").strip()
    if not secret_id or not secret_key or not group_id:
        raise FaceProviderNotConfigured("missing_credentials", "腾讯云人脸识别未完整配置")

    from tencentcloud.common import credential
    from tencentcloud.common.profile.client_profile import ClientProfile
    from tencentcloud.common.profile.http_profile import HttpProfile
    from tencentcloud.iai.v20200303 import iai_client

    timeout = float(os.getenv("GO2_FACE_TIMEOUT_S", "5"))
    http = HttpProfile(endpoint="iai.tencentcloudapi.com")
    http.reqTimeout = max(1, int(timeout))
    profile = ClientProfile(httpProfile=http)
    client = iai_client.IaiClient(
        credential.Credential(secret_id, secret_key),
        os.getenv("TENCENTCLOUD_REGION", "ap-shanghai"),
        profile,
    )
    return TencentFaceProvider(
        client,
        group_id,
        timeout=timeout,
        face_match_threshold=float(os.getenv("GO2_FACE_CANDIDATE_SCORE", "80")),
    )
