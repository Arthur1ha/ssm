"""Identity matching has one fixed threshold and preserves unknown/error meaning."""

from types import SimpleNamespace
import time
from unittest.mock import AsyncMock

import cv2
import numpy as np
import pytest

from cloud.go2.perception import identity
from cloud.space.people.memory import KnownPerson, PeopleMemory
from cloud.space.people.provider import FaceCandidate, FaceSearchResult
from cloud.space.people.tencent import TencentFaceProvider


@pytest.fixture
def recognition(tmp_path):
    people = PeopleMemory(tmp_path / "people.json")
    person = people.upsert(KnownPerson("owner", "主人", "owner", "tencent_iai", "group", 1, 1, 1))
    ok, encoded = cv2.imencode(".jpg", np.zeros((200, 200, 3), dtype=np.uint8))
    assert ok
    frame = {"frame_id": 1, "ts": time.time(),
             "faces": {"count": 1, "boxes": [(30, 30, 100, 100)]}}
    provider = SimpleNamespace(search_persons=AsyncMock())
    skill = identity.IdentitySkill(provider, people, lambda: (frame, encoded.tobytes()))
    return skill, provider, person, frame


@pytest.mark.parametrize("score,expected", [(79.99, "unknown"), (80, "recognized"),
                                           (85, "recognized"), (90, "recognized")])
async def test_fixed_threshold_replaces_previous_identity(recognition, score, expected):
    skill, provider, person, _ = recognition
    skill._current = skill._recognized(person, 99)
    provider.search_persons.return_value = FaceSearchResult((FaceCandidate("owner", "主人", score),))
    result = await skill.recognize_now()
    assert result.status == expected
    assert result.score == score
    assert (result.person_id, result.name, result.role) == (
        ("owner", "主人", "owner") if expected == "recognized" else ("", "陌生人", "stranger"))
    provider.search_persons.assert_awaited_once()


@pytest.mark.parametrize("candidates", [(), (FaceCandidate("remote-only", "其他人员", 99),)])
async def test_no_registered_match_returns_stranger(recognition, candidates):
    skill, provider, _, _ = recognition
    provider.search_persons.return_value = FaceSearchResult(candidates)
    result = await skill.recognize_now()
    assert (result.status, result.person_id, result.name, result.role) == (
        "unknown", "", "陌生人", "stranger")


@pytest.mark.parametrize("boxes,code", [([], "no_face"),
                                       ([(0, 0, 100, 100)] * 2, "multiple_faces"),
                                       ([(0, 0, 79, 100)], "face_too_small")])
async def test_face_gate_errors_are_not_strangers(recognition, boxes, code):
    skill, provider, _, frame = recognition
    frame["faces"].update(count=len(boxes), boxes=boxes)
    result = await skill.recognize_now()
    assert (result.status, result.error_code, result.name) == ("error", code, "")
    provider.search_persons.assert_not_awaited()


async def test_legacy_score_configuration_does_not_change_matching(recognition, monkeypatch):
    _, provider, _, _ = recognition
    monkeypatch.setenv("GO2_FACE_ACCEPT_SCORE", "99")
    monkeypatch.setenv("GO2_FACE_CANDIDATE_SCORE", "99")
    monkeypatch.setenv("GO2_FACE_RECOGNITION_ENABLED", "true")
    provider.search_persons.return_value = FaceSearchResult((FaceCandidate("owner", "主人", 80),))
    monkeypatch.setattr(identity, "space_people", SimpleNamespace(
        provider=provider, people=recognition[0].people))
    monkeypatch.setattr(identity, "_default_frame_getter", recognition[0]._frame_getter)
    assert (await identity._build_default_identity_skill().recognize_now()).status == "recognized"


async def test_tencent_search_uses_same_fixed_threshold(monkeypatch):
    monkeypatch.setenv("GO2_FACE_CANDIDATE_SCORE", "99")
    provider = TencentFaceProvider(SimpleNamespace(SearchPersons=object()), "group")
    call = AsyncMock(return_value=SimpleNamespace(Results=[], RequestId="request"))
    monkeypatch.setattr(provider, "_call", call)
    await provider.search_persons(b"jpeg")
    request = call.await_args.args[1]
    assert request.FaceMatchThreshold == 80
