import cv2
import numpy as np
import pytest
import asyncio

from cloud.go2.agentcore.identity.provider import (
    AddedFace, CreatedPerson, FaceCandidate, FaceProviderUnavailable, FaceSearchResult,
)
from cloud.go2.agentcore.memory.people import KnownPerson, PeopleMemory
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
        self.created = []
        self.added = []
        self.removed = []

    async def search_persons(self, image):
        return self.results.pop(0) if self.results else FaceSearchResult(())

    async def create_person(self, person_id, name, image):
        self.created.append((person_id, name, image))
        return CreatedPerson(person_id, "f1")

    async def add_face(self, person_id, image):
        self.added.append(person_id)
        return AddedFace(person_id, ("f2",))

    async def remove_from_group(self, person_id):
        self.removed.append(person_id)


def known(person_id="go2_owner", face_count=1):
    return KnownPerson(person_id, "主人", "owner", "tencent_iai", "123456", face_count, 1, 1)


def make_skill(tmp_path, provider=None, enrollment=True, current_frame=None):
    provider = provider or FakeProvider()
    current_frame = current_frame or frame()
    memory = PeopleMemory(tmp_path / "people.json")
    skill = IdentitySkill(
        provider, memory, lambda: (current_frame, jpeg()),
        enrollment_enabled=enrollment, recheck_s=30,
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
async def test_unique_owner_and_face_limit(tmp_path):
    skill, provider, memory = make_skill(tmp_path)
    enrolled = await skill.remember_person("主人", "owner")
    assert enrolled.status == "recognized"
    assert provider.created[0][0] == "go2_owner"
    assert (await skill.remember_person("另一个主人", "owner")).status == "rejected"
    memory.upsert(known(face_count=5))
    limited = await skill.add_person_face("go2_owner")
    assert limited.error_code == "face_limit"
    assert provider.added == []


@pytest.mark.asyncio
async def test_multi_face_rejected_and_disabled_isolated(tmp_path):
    skill, provider, _ = make_skill(tmp_path, current_frame=frame(2))
    assert (await skill.remember_person("朋友", "friend")).error_code == "multiple_faces"
    disabled = IdentitySkill(None, PeopleMemory(tmp_path / "disabled.json"), lambda: (None, None))
    assert (await disabled.recognize_now()).status == "disabled"


@pytest.mark.asyncio
async def test_delete_remote_before_local(tmp_path):
    skill, provider, memory = make_skill(tmp_path)
    memory.upsert(known())
    await skill.forget_person("go2_owner")
    assert provider.removed == ["go2_owner"]
    assert memory.get("go2_owner") is None


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


@pytest.mark.asyncio
async def test_remote_create_failure_does_not_write_local_person(tmp_path):
    class BrokenProvider(FakeProvider):
        async def create_person(self, person_id, name, image):
            raise FaceProviderUnavailable("rate_limited", "暂时不可用")

    skill, _, memory = make_skill(tmp_path, provider=BrokenProvider())
    result = await skill.remember_person("主人", "owner")
    assert result.status == "error"
    assert result.error_code == "rate_limited"
    assert memory.list() == ()
