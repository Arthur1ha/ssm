from .conftest import login_admin
import pytest


@pytest.mark.asyncio
async def test_devices_require_session_and_include_offline_fixed_devices(client):
    assert (await client.get("/api/devices")).status_code == 401
    await login_admin(client)
    response = await client.get("/api/devices")
    assert response.status_code == 200
    cards = {item["unit_id"]: item for item in response.json()}
    assert cards["esp32_desk"]["online"] is False
    assert cards["go2"]["online"] is False


@pytest.mark.asyncio
async def test_legacy_space_and_scope_query_are_rejected(client):
    await login_admin(client)
    response = await client.get("/api/devices?scope=adopted&space_id=0")
    assert response.status_code == 400
