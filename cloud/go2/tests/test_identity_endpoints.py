import pytest
from unittest.mock import MagicMock

import cloud.go2.router as router_mod
from cloud.go2.agentcore.skills.identity import IdentitySnapshot


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

    fake_loop.add_callback.assert_called_once_with(router_mod._on_identity_vision_frame)
    fake_loop.start.assert_called_once()
    fake_drive.start.assert_not_called()


