import json

import httpx
import pytest

from cloud.cards.registry import CardRegistry
from cloud.space.device_codes import DeviceCodeStore, get_device_code_store
from cloud.space.registry import SpaceRegistry
from edge.lamp_contract import FSM_VERSION, capabilities
from .conftest import login_admin


@pytest.mark.asyncio
async def test_create_space_enters_main_without_admin_password(client):
    response = await client.post("/api/admin/session")
    assert response.status_code == 200
    assert response.json()["actor"] == "admin"
    assert response.json()["space_id"] == "main"


@pytest.mark.asyncio
async def test_device_code_persists_rotates_and_bind_is_idempotent(client, api_env, monkeypatch):
    space, _ = api_env
    csrf = await login_admin(client)
    space.update_space(device_ids=[])
    cards = CardRegistry()
    monkeypatch.setattr("cloud.cards.registry._registry", cards)
    store = get_device_code_store()
    first = (await client.get("/api/admin/device-codes/esp32_desk")).json()
    assert first == (await client.get("/api/admin/device-codes/esp32_desk")).json()
    assert DeviceCodeStore(store.path).validate(first["code"]) == "esp32_desk"
    assert (await client.post("/api/admin/devices/bind", json={"code": first["code"]}, headers=csrf)).status_code == 404
    cards.handle_message("ssm/agents/esp32_desk_light/manifest", '{"unit_id":"esp32_desk_light","device_id":"esp32_desk","agent_type":"sensor","name":"Light"}')
    assert (await client.post("/api/admin/devices/bind", json={"code": first["code"]}, headers=csrf)).status_code == 404
    cards.handle_message("ssm/agents/esp32_desk_led/manifest", json.dumps({
        "unit_id": "esp32_desk_led", "parent_id": "esp32_desk", "agent_type": "actuator",
        "name": "Desk light", "firmware_ver": "0.3.0",
        "fsm_version": FSM_VERSION, "capabilities": capabilities(),
    }), retained=True)
    cards.handle_message("ssm/agents/esp32_desk/status", "online", retained=True)
    assert (await client.get("/api/devices")).json() == []
    assert (await client.post("/api/admin/devices/bind", json={"code": first["code"]})).status_code == 403
    bound = await client.post("/api/admin/devices/bind", json={"code": first["code"]}, headers=csrf)
    assert bound.status_code == 200
    assert bound.json()["result"] == "added"
    assert bound.json()["cards"][0]["unit_id"] == "esp32_desk_led"
    visible = (await client.get("/api/devices")).json()
    assert [card["unit_id"] for card in visible] == ["esp32_desk_led"]
    assert visible[0]["online"] is True
    again = await client.post("/api/admin/devices/bind", json={"code": first["code"]}, headers=csrf)
    assert again.json()["result"] == "already_added"
    assert space.get_space()["device_ids"] == ["esp32_desk"]
    rotated = (await client.post("/api/admin/device-codes/esp32_desk/rotate", headers=csrf)).json()
    assert rotated["code"] != first["code"]
    assert store.validate(first["code"]) is None
    assert store.validate(rotated["code"]) == "esp32_desk"
    assert (await client.post("/api/admin/devices/bind", json={"code": first["code"]}, headers=csrf)).status_code == 400
    assert (await client.post("/api/admin/devices/bind", json={"code": "ssm-device:v1:esp32_desk:无效标签"}, headers=csrf)).status_code == 400


@pytest.mark.asyncio
async def test_device_binding_rejects_visitor_code_and_visitor_session(client, app):
    csrf = await login_admin(client)
    code = (await client.get("/api/admin/device-codes/go2")).json()["code"]
    assert (await client.post("/api/admin/devices/bind", json={"code": "ssm-invite:v1:abc"}, headers=csrf)).status_code == 400
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as anon:
        assert (await anon.get("/api/admin/device-codes/go2")).status_code == 401
        assert (await anon.post("/api/admin/devices/bind", json={"code": code})).status_code == 401
        invite = (await client.post("/api/admin/invite/enable", headers=csrf)).json()
        assert (await anon.post("/api/visitor/session", json={"token": invite["share_id"]})).status_code == 200
        assert (await anon.post("/api/admin/devices/bind", json={"code": code}, headers={
            "X-CSRF-Token": anon.cookies.get("ssm_csrf"),
        })).status_code == 403


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


@pytest.mark.asyncio
async def test_remove_device_preserves_other_members_settings_and_label(client, api_env):
    space, mqtt = api_env
    csrf = await login_admin(client)
    space.update_space(name="Keep settings", device_ids=["esp32_desk", "go2"])
    label = get_device_code_store().get("esp32_desk")
    invite = space.get_invite()
    assert (await client.delete("/api/admin/devices/esp32_desk")).status_code == 403
    response = await client.delete("/api/admin/devices/esp32_desk", headers=csrf)
    assert response.json() == {"device_id": "esp32_desk", "result": "removed"}
    assert space.get_space()["device_ids"] == ["go2"]
    assert space.get_space()["name"] == "Keep settings"
    assert space.get_invite() == invite
    assert get_device_code_store().get("esp32_desk") == label
    assert not mqtt.messages  # Membership changes never send physical commands.
    assert (await client.delete("/api/admin/devices/esp32_desk", headers=csrf)).json()["result"] == "already_removed"
    assert (await client.delete("/api/admin/devices/unknown", headers=csrf)).status_code == 404


@pytest.mark.asyncio
async def test_remove_device_rejects_anonymous_and_visitor(client, app, api_env):
    space, _ = api_env
    space.add_device("go2")
    csrf = await login_admin(client)
    invite = (await client.post("/api/admin/invite/enable", headers=csrf)).json()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as visitor:
        assert (await visitor.delete("/api/admin/devices/go2")).status_code == 401
        await visitor.post("/api/visitor/session", json={"token": invite["share_id"]})
        assert (await visitor.delete("/api/admin/devices/go2", headers={
            "X-CSRF-Token": visitor.cookies.get("ssm_csrf"),
        })).status_code == 403
    assert space.get_space()["device_ids"] == ["go2"]
