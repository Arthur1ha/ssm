from types import SimpleNamespace

import pytest

import cloud.go2.perception.identity as identity_mod
import cloud.space.people.service as people_mod
from cloud.go2.tools.tools import (
    go2_list_people,
    go2_recognize_person,
)


class FakeIdentity:
    async def recognize_now(self):
        return SimpleNamespace(status="unknown", person_id="", name="陌生人", role="stranger", score=0, ts=1, error_code="")

    def list_people(self):
        return (SimpleNamespace(person_id="go2_owner", name="主人", role="owner", face_count=2),)


@pytest.fixture(autouse=True)
def fake_identity(monkeypatch):
    fake = FakeIdentity()
    monkeypatch.setattr(identity_mod, "identity_skill", fake)
    monkeypatch.setattr(people_mod, "space_people", fake)
    return fake


@pytest.mark.asyncio
async def test_identity_tools_return_structured_status(fake_identity):
    result = await go2_recognize_person()
    assert result["status"] == "unknown"
    assert result["name"] == "陌生人"
    assert result["role"] == "stranger"
    assert go2_list_people()["people"][0]["role"] == "owner"
