import cv2
import numpy as np
import pytest
import asyncio

from cloud.space.people.provider import (
    FaceCandidate, FaceSearchResult,
)
from cloud.space.people.memory import KnownPerson, PeopleMemory
from cloud.go2.agentcore.skills.identity import IdentitySkill


def jpeg():
    ok, encoded = cv2.imencode(".jpg", np.zeros((200, 200, 3), dtype=np.uint8))
    assert ok
    return encoded.tobytes()


def frame(count=1):
    boxes = [(30, 30, 100, 100)] * count
    return {
        "frame_id": 1, "ts": 0.0,
        "persons": {"detected": count > 0, "count": count},
        "faces": {"detected": count > 0, "count": count, "boxes": boxes},
        "changed": False, "change_type": "none",
    }


class FakeProvider:
    group_id = "123456"

    def __init__(self):
        self.results = []

    async def search_persons(self, image):
        return self.results.pop(0) if self.results else FaceSearchResult(())


def known(person_id="go2_owner", face_count=1):
    return KnownPerson(person_id, "主人", "owner", "tencent_iai", "123456", face_count, 1, 1)


def make_skill(tmp_path, provider=None, current_frame=None):
    provider = provider or FakeProvider()
    current_frame = current_frame or frame()
    memory = PeopleMemory(tmp_path / "people.json")
    skill = IdentitySkill(
        provider, memory, lambda: (current_frame, jpeg()), recheck_s=30,
    )
    return skill, provider, memory


@pytest.mark.asyncio
async def test_high_score_recognizes_local_person(tmp_path):
    skill, provider, memory = make_skill(tmp_path)
    memory.upsert(known())
    provider.results = [FaceSearchResult((FaceCandidate("go2_owner", "主人", 96.2),))]
    result = await skill.recognize_now()
    assert result.status == "recognized"
    assert result.role == "owner"
    assert result.score == 96.2


@pytest.mark.asyncio
async def test_mid_score_needs_two_consecutive_matches(tmp_path):
    skill, provider, memory = make_skill(tmp_path)
    memory.upsert(known())
    candidate = FaceSearchResult((FaceCandidate("go2_owner", "主人", 85),))
    provider.results = [candidate, candidate]
    assert (await skill.recognize_now()).status == "candidate"
    assert (await skill.recognize_now()).status == "recognized"


@pytest.mark.asyncio
async def test_disabled_recognition_is_isolated(tmp_path):
    disabled = IdentitySkill(None, PeopleMemory(tmp_path / "disabled.json"), lambda: (None, None))
    assert (await disabled.recognize_now()).status == "disabled"


@pytest.mark.asyncio
async def test_singleflight_drops_second_request(tmp_path):
    gate = asyncio.Event()

    class SlowProvider(FakeProvider):
        async def search_persons(self, image):
            await gate.wait()
            return FaceSearchResult(())

    skill, _, _ = make_skill(tmp_path, provider=SlowProvider())
    first = asyncio.create_task(skill.recognize_now())
    await asyncio.sleep(0)
    second = await skill.recognize_now()
    assert second.status == "searching"
    assert second.error_code == "busy"
    gate.set()
    assert (await first).status == "unknown"


def test_face_absence_clears_recognized_identity(tmp_path):
    skill, _, memory = make_skill(tmp_path)
    person = memory.upsert(known())
    skill._current = skill._recognized(person, 99)
    skill._previous_face_count = 0
    skill._last_face_seen = 0
    skill.on_vision_frame(frame(0))
    assert skill.current.status == "idle"
