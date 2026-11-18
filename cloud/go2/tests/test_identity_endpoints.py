import pytest
from unittest.mock import MagicMock

import cloud.go2.router as router_mod
from cloud.go2 import service
from cloud.go2.perception.identity import IdentitySnapshot


class FakeIdentity:
    enabled = True

    def __init__(self):
        self.current = IdentitySnapshot("recognized", "go2_owner", "主人", "owner", 96.2, 1, "")

    async def recognize_now(self):
        return self.current


@pytest.fixture
def fake_identity(monkeypatch):
    fake = FakeIdentity()
    monkeypatch.setattr(router_mod, "identity_skill", fake)
    return fake


@pytest.mark.asyncio
async def test_identity_status(fake_identity):
    assert (await router_mod.go2_identity_get())["role"] == "owner"


@pytest.mark.asyncio
async def test_identity_recognize(fake_identity):
    response = await router_mod.go2_identity_recognize()
    assert response["status"] == "recognized"


def test_manual_mode_keeps_identity_and_teaching_vision_consumers(monkeypatch):
    fake_loop = MagicMock()
    fake_identity = MagicMock(enabled=True)
    fake_drive = MagicMock()
    monkeypatch.setattr(service, "vision_loop", fake_loop)
    monkeypatch.setattr(service, "identity_skill", fake_identity)
    monkeypatch.setattr(service, "drive", fake_drive)
    monkeypatch.setattr(service.go2, "is_connected", True)
    monkeypatch.setattr(service, "_active_rule_cb", None)
    monkeypatch.setattr(service, "_active_drive_cb", None)
    monkeypatch.setattr(service, "_active_identity_cb", None)

    router_mod._configure_vision_consumers("manual")

    fake_loop.add_callback.assert_any_call(service._on_identity_vision_frame)
    fake_loop.add_callback.assert_any_call(service.person_teachings.on_vision_frame)
    assert fake_loop.add_callback.call_count == 2
    fake_loop.start.assert_called_once()
    fake_drive.start.assert_not_called()
