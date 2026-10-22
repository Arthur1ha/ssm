import asyncio
import base64
from types import SimpleNamespace

import pytest
from tencentcloud.common.exception.tencent_cloud_sdk_exception import TencentCloudSDKException
from tencentcloud.iai.v20200303 import models

from cloud.space.people.provider import (
    DuplicateFaceCandidate,
    FaceImageRejected,
    FaceProviderUnavailable,
)
from cloud.space.people.tencent import TencentFaceProvider


class FakeClient:
    def __init__(self):
        self.requests = []
        self.response = None
        self.error = None

    def _call(self, req):
        self.requests.append(req)
        if self.error:
            raise self.error
        return self.response

    GetGroupInfo = CreatePerson = CreateFace = SearchPersons = GetPersonList = DeletePersonFromGroup = _call


@pytest.mark.asyncio
async def test_create_person_request_and_duplicate_are_normalized():
    client = FakeClient()
    response = models.CreatePersonResponse()
    response.FaceId = "face-1"
    response.SimilarPersonId = ""
    response.RequestId = "req-1"
    client.response = response
    provider = TencentFaceProvider(client, "123456")

    result = await provider.create_person("go2_owner", "主人", b"jpeg")
    request = client.requests[-1]
    assert request.GroupId == "123456"
    assert request.PersonId == "go2_owner"
    assert request.Image == base64.b64encode(b"jpeg").decode()
    assert request.QualityControl == 3
    assert request.UniquePersonControl == 3
    assert result.face_id == "face-1"

    response.SimilarPersonId = "already-there"
    with pytest.raises(DuplicateFaceCandidate) as exc:
        await provider.create_person("new", "新朋友", b"secret-image")
    assert exc.value.person_id == "already-there"
    assert "secret-image" not in str(exc.value)


@pytest.mark.asyncio
async def test_search_request_and_candidates_are_normalized():
    client = FakeClient()
    candidate = models.Candidate()
    candidate.PersonId = "p1"
    candidate.PersonName = "朋友"
    candidate.Score = 91.5
    result = models.Result()
    result.RetCode = 0
    result.Candidates = [candidate]
    response = models.SearchPersonsResponse()
    response.Results = [result]
    response.RequestId = "req-search"
    client.response = response
    provider = TencentFaceProvider(client, "123456", face_match_threshold=81)

    found = await provider.search_persons(b"jpeg")
    request = client.requests[-1]
    assert request.GroupIds == ["123456"]
    assert request.MaxFaceNum == 1
    assert request.MaxPersonNum == 3
    assert request.MinFaceSize == 80
    assert request.QualityControl == 2
    assert request.FaceMatchThreshold == 81
    assert found.candidates[0].person_id == "p1"
    assert found.request_id == "req-search"


@pytest.mark.asyncio
async def test_group_must_be_face_model_3():
    client = FakeClient()
    response = models.GetGroupInfoResponse()
    response.GroupId = "123456"
    response.GroupName = "go2人员库"
    response.FaceModelVersion = "2.0"
    response.RequestId = "req-group"
    client.response = response
    with pytest.raises(FaceProviderUnavailable) as exc:
        await TencentFaceProvider(client, "123456").check_group()
    assert exc.value.code == "unsupported_face_model"


@pytest.mark.asyncio
async def test_sdk_errors_and_timeout_are_safe():
    client = FakeClient()
    client.error = TencentCloudSDKException("InvalidParameterValue.NoFaceInPhoto", "raw detail", "req-x")
    provider = TencentFaceProvider(client, "123456")
    with pytest.raises(FaceImageRejected) as exc:
        await provider.search_persons(b"jpeg")
    assert exc.value.request_id == "req-x"
    assert "jpeg" not in str(exc.value)

    class SlowClient(FakeClient):
        def _call(self, req):
            import time
            time.sleep(0.05)

        SearchPersons = _call

    with pytest.raises(FaceProviderUnavailable) as exc:
        await TencentFaceProvider(SlowClient(), "123456", timeout=0.001).search_persons(b"jpeg")
    assert exc.value.code == "timeout"
