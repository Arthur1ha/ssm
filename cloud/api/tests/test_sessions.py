import httpx
import pytest

from cloud.api import intents
from .conftest import login_admin


@pytest.mark.asyncio
async def test_admin_cookie_is_signed_httponly_and_csrf_protected(client):
    response = await client.post("/api/admin/session")
    assert response.status_code == 200
    cookies = response.headers.get_list("set-cookie")
    assert any("ssm_session=" in value and "HttpOnly" in value and "SameSite=lax" in value for value in cookies)
    assert any("ssm_csrf=" in value and "HttpOnly" not in value for value in cookies)

    assert (await client.post("/api/admin/invite/rotate")).status_code == 403
    csrf = {"X-CSRF-Token": client.cookies.get("ssm_csrf")}
    assert (await client.post("/api/admin/invite/rotate", headers=csrf)).status_code == 200


@pytest.mark.asyncio
async def test_invite_hash_rotation_disable_and_visitor_invalidation(client, app, api_env):
    store, _mqtt = api_env
    csrf = await login_admin(client)
    first = (await client.post("/api/admin/invite/rotate", headers=csrf)).json()["token"]
    assert first not in store.path.read_text(encoding="utf-8")

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as visitor:
        exchanged = await visitor.post("/api/visitor/session", json={"token": first})
        assert exchanged.status_code == 200
        assert exchanged.json()["actor"] == "visitor"

        second = (await client.post("/api/admin/invite/rotate", headers=csrf)).json()["token"]
        assert second != first
        assert (await visitor.get("/api/me")).status_code == 401
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as stale:
            assert (await stale.post("/api/visitor/session", json={"token": first})).status_code == 401

        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as active:
            assert (await active.post("/api/visitor/session", json={"token": second})).status_code == 200
            await client.post("/api/admin/invite/disable", headers=csrf)
            assert (await active.get("/api/me")).status_code == 401


@pytest.mark.asyncio
async def test_admin_only_space_and_fixed_id(client, api_env):
    api_env[0].update_space(device_ids=["go2"])
    csrf = await login_admin(client)
    response = await client.patch(
        "/api/admin/space", json={"name": "Demo"}, headers=csrf,
    )
    assert response.status_code == 200
    assert response.json()["id"] == "main"
    assert response.json()["name"] == "Demo"
    assert (await client.patch(
        "/api/admin/space", json={"id": "other"}, headers=csrf,
    )).status_code == 422
    assert (await client.patch(
        "/api/admin/space", json={"device_ids": ["go2"]}, headers=csrf,
    )).status_code == 200


@pytest.mark.asyncio
async def test_admin_can_manage_devices_in_main_space(client, api_env):
    api_env[0].update_space(device_ids=["esp32_desk", "go2"])
    csrf = await login_admin(client)
    candidates = await client.get("/api/admin/device-candidates")
    assert candidates.status_code == 200
    assert {row["device_id"] for row in candidates.json()} == {"esp32_desk", "go2"}

    response = await client.patch(
        "/api/admin/space", json={"device_ids": ["go2"]}, headers=csrf,
    )
    assert response.status_code == 200
    assert response.json()["device_ids"] == ["go2"]
    assert (await client.get("/api/devices")).json()[0]["device_id"] == "go2"

    empty = await client.patch(
        "/api/admin/space", json={"device_ids": []}, headers=csrf,
    )
    assert empty.status_code == 200
    assert empty.json()["device_ids"] == []

    forbidden_add = await client.patch(
        "/api/admin/space", json={"device_ids": ["go2"]}, headers=csrf,
    )
    assert forbidden_add.status_code == 400

    unsupported = await client.patch(
        "/api/admin/space", json={"device_ids": ["other"]}, headers=csrf,
    )
    assert unsupported.status_code == 400


@pytest.mark.asyncio
async def test_create_admin_session_from_homepage(client):
    created = await client.post("/api/admin/session")
    assert created.status_code == 200
    assert created.json()["actor"] == "admin"
    assert (await client.get("/api/me")).status_code == 200


@pytest.mark.asyncio
async def test_admin_session_works_without_configured_session_secret(client, monkeypatch):
    monkeypatch.delenv("SSM_SESSION_SECRET", raising=False)
    response = await client.post("/api/admin/session")
    assert response.status_code == 200
    assert (await client.get("/api/me")).status_code == 200


@pytest.mark.asyncio
async def test_valid_cookie_restores_cleanup_watcher_after_process_restart(client, monkeypatch):
    response = await client.post(
        "/api/admin/session",
    )
    assert response.status_code == 200
    session_id = response.json()["session_id"]

    # Model the in-memory state after an API process restart while retaining
    # the browser's signed cookie.
    intents._cancel_cleanup_task(session_id)
    restored = []
    monkeypatch.setattr(
        intents, "schedule_session_cleanup",
        lambda session: restored.append(session["session_id"]),
    )

    assert (await client.get("/api/me")).status_code == 200
    assert restored == [session_id]
