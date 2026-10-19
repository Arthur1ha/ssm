from types import SimpleNamespace

import pytest

import cloud.go2.agentcore.skills.identity as identity_mod
from cloud.go2.agentcore.tools.tools import (
    go2_add_person_face,
    go2_forget_person,
    go2_list_people,
    go2_recognize_person,
    go2_remember_person,
)


class FakeIdentity:
    async def remember_person(self, name, role):
        return SimpleNamespace(status="recognized", person_id="go2_owner", name=name, role=role, score=100, ts=1, error_code="")

    async def add_person_face(self, person_id):
        return SimpleNamespace(status="rejected", person_id=person_id, name="", role="", score=0, ts=1, error_code="face_limit")

    async def recognize_now(self):
        return SimpleNamespace(status="unknown", person_id="", name="", role="", score=0, ts=1, error_code="")

    def list_people(self):
        return (SimpleNamespace(person_id="go2_owner", name="主人", role="owner", face_count=2),)

    async def forget_person(self, person_id):
        self.forgot = person_id


@pytest.fixture(autouse=True)
def fake_identity(monkeypatch):
    fake = FakeIdentity()
    monkeypatch.setattr(identity_mod, "identity_skill", fake)
    return fake


@pytest.mark.asyncio
async def test_identity_tools_return_structured_status(fake_identity):
    assert (await go2_remember_person("主人", "owner"))["result"] == "ok"
    assert (await go2_add_person_face("go2_owner"))["result"] == "rejected"
    assert (await go2_recognize_person())["status"] == "unknown"
    assert go2_list_people()["people"][0]["role"] == "owner"
    assert (await go2_forget_person("go2_owner"))["result"] == "ok"
    assert fake_identity.forgot == "go2_owner"
