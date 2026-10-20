import httpx
import pytest

from cloud.api import intents
from .conftest import login_admin


@pytest.mark.asyncio
async def test_admin_cookie_is_signed_httponly_and_csrf_protected(client):
    response = await client.post("/api/admin/session", json={"access_code": "correct horse battery staple"})
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
async def test_admin_only_space_and_fixed_id(client):
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
    )).status_code == 400


@pytest.mark.asyncio
async def test_bad_login_and_missing_session(client):
    assert (await client.post("/api/admin/session", json={"access_code": "wrong"})).status_code == 401
    assert (await client.get("/api/me")).status_code == 401


@pytest.mark.asyncio
async def test_valid_cookie_restores_cleanup_watcher_after_process_restart(client, monkeypatch):
    response = await client.post(
        "/api/admin/session", json={"access_code": "correct horse battery staple"},
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
