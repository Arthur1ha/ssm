from dataclasses import replace

import pytest
from unittest.mock import MagicMock

import cloud.go2.router as router_mod
from cloud.go2.agentcore.memory.people import KnownPerson
from cloud.go2.agentcore.skills.identity import IdentitySnapshot


class FakeIdentity:
    enabled = True

    def __init__(self):
        self.current = IdentitySnapshot("recognized", "go2_owner", "主人", "owner", 96.2, 1, "")
        self.people = [KnownPerson("go2_owner", "主人", "owner", "tencent_iai", "123456", 1, 1, 1)]

    async def recognize_now(self):
        return self.current

    def list_people(self):
        return tuple(self.people)

    async def remember_person(self, name, role):
        return replace(self.current, name=name, role=role)

    async def add_person_face(self, person_id):
        return self.current

    async def forget_person(self, person_id):
        self.people = [person for person in self.people if person.person_id != person_id]


@pytest.fixture
def fake_identity(monkeypatch):
    fake = FakeIdentity()
    monkeypatch.setattr(router_mod, "identity_skill", fake)
    return fake


def test_identity_status(fake_identity):
    assert router_mod.go2_identity_get()["role"] == "owner"


@pytest.mark.asyncio
async def test_identity_recognize(fake_identity):
    response = await router_mod.go2_identity_recognize()
    assert response["status"] == "recognized"


@pytest.mark.asyncio
async def test_people_crud_does_not_expose_cloud_face_ids(fake_identity):
    listed = router_mod.go2_people_list()
    assert set(listed[0]) == {"person_id", "name", "role", "face_count"}
    request = router_mod.PersonCreateRequest(name="主人", role="owner")
    assert (await router_mod.go2_people_create(request))["status"] == "recognized"
    assert (await router_mod.go2_people_add_face("go2_owner"))["status"] == "recognized"
    assert (await router_mod.go2_people_delete("go2_owner"))["ok"] is True


def test_manual_mode_keeps_only_identity_vision_consumer(monkeypatch):
    fake_loop = MagicMock()
    fake_identity = MagicMock(enabled=True)
    fake_drive = MagicMock()
    monkeypatch.setattr(router_mod, "vision_loop", fake_loop)
    monkeypatch.setattr(router_mod, "identity_skill", fake_identity)
    monkeypatch.setattr(router_mod, "drive", fake_drive)
    monkeypatch.setattr(router_mod.go2, "is_connected", True)
    monkeypatch.setattr(router_mod, "_active_rule_cb", None)
    monkeypatch.setattr(router_mod, "_active_drive_cb", None)
    monkeypatch.setattr(router_mod, "_active_identity_cb", None)

    router_mod._configure_vision_consumers("manual")

    fake_loop.add_callback.assert_called_once_with(fake_identity.on_vision_frame)
    fake_loop.start.assert_called_once()
    fake_drive.start.assert_not_called()


