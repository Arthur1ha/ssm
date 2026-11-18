import httpx
import pytest

from cloud.cards.registry import get_registry
from cloud.space.device_codes import DeviceCodeStore, get_device_code_store
from cloud.space.registry import SpaceRegistry
from .conftest import login_admin


@pytest.mark.asyncio
async def test_create_space_enters_main_without_admin_password(client):
    response = await client.post("/api/admin/session")
    assert response.status_code == 200
    assert response.json()["actor"] == "admin"
    assert response.json()["space_id"] == "main"


@pytest.mark.asyncio
async def test_device_code_persists_rotates_and_bind_is_idempotent(client, api_env):
    space, _ = api_env
    csrf = await login_admin(client)
    space.update_space(device_ids=[])
    store = get_device_code_store()
    first = (await client.get("/api/admin/device-codes/esp32_desk")).json()
    assert first == (await client.get("/api/admin/device-codes/esp32_desk")).json()
    assert DeviceCodeStore(store.path).validate(first["code"]) == "esp32_desk"
    assert (await client.post("/api/admin/devices/bind", json={"code": first["code"]}, headers=csrf)).status_code == 404
    cards = get_registry()
    cards.handle_message("ssm/agents/esp32_desk_light/manifest", '{"unit_id":"esp32_desk_light","device_id":"esp32_desk","agent_type":"sensor","name":"Light"}')
    # A published Card is the authority for capabilities; use the registry's
    # parsed Card representation so the test does not depend on manifest shape.
    with cards._lock:
        cards._cards["esp32_desk_light"] = {
            "unit_id": "esp32_desk_light", "device_id": "esp32_desk",
            "name": "Desk light", "agent_type": "actuator", "online": False,
        }
    try:
        bound = await client.post("/api/admin/devices/bind", json={"code": first["code"]}, headers=csrf)
        assert bound.status_code == 200
        assert bound.json()["result"] == "added"
        assert bound.json()["cards"][0]["unit_id"] == "esp32_desk_light"
        again = await client.post("/api/admin/devices/bind", json={"code": first["code"]}, headers=csrf)
        assert again.json()["result"] == "already_added"
        assert space.get_space()["device_ids"] == ["esp32_desk"]
        rotated = (await client.post("/api/admin/device-codes/esp32_desk/rotate", headers=csrf)).json()
        assert rotated["code"] != first["code"]
        assert store.validate(first["code"]) is None
        assert store.validate(rotated["code"]) == "esp32_desk"
    finally:
        with cards._lock:
            cards._cards.pop("esp32_desk_light", None)


@pytest.mark.asyncio
async def test_device_binding_rejects_visitor_code_and_visitor_session(client, app):
    csrf = await login_admin(client)
    code = (await client.get("/api/admin/device-codes/go2")).json()["code"]
    assert (await client.post("/api/admin/devices/bind", json={"code": "ssm-invite:v1:abc"}, headers=csrf)).status_code == 400
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as anon:
        assert (await anon.get("/api/admin/device-codes/go2")).status_code == 401
        assert (await anon.post("/api/admin/devices/bind", json={"code": code})).status_code == 401


def test_existing_space_members_survive_registry_restart(tmp_path):
    path = tmp_path / "space.json"
    first = SpaceRegistry(path)
    assert first.get_space()["device_ids"] == []
    first.update_space(name="Existing", device_ids=["go2"])
    restored = SpaceRegistry(path)
    assert restored.get_space()["device_ids"] == ["go2"]
    assert restored.get_space()["name"] == "Existing"
    assert restored.add_device("go2") is False
    assert restored.add_device("esp32_desk") is True
    assert restored.get_space()["name"] == "Existing"
