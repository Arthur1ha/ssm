"""灯智能体 chat / memory HTTP 端点测试。"""
import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI

from cloud.esp32 import router as esp32_router
from cloud.esp32 import service as agent_mod
from cloud.api import intents
from cloud.api.sessions import router as sessions_router


class _FakeAgent:
    _belief_summary = "傍晚常调暖光"


@pytest_asyncio.fixture
async def client(monkeypatch):
    monkeypatch.setenv("SSM_SESSION_SECRET", "s" * 48)
    monkeypatch.setenv("SSM_COOKIE_SECURE", "false")
    app = FastAPI()
    app.include_router(sessions_router)
    app.include_router(esp32_router.router)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as value:
        yield value
    for session_id in tuple(intents._cleanup_tasks):
        intents._cancel_cleanup_task(session_id)


async def _login_admin(client):
    response = await client.post("/api/admin/session")
    assert response.status_code == 200
    return {"X-CSRF-Token": client.cookies.get("ssm_csrf")}


@pytest.mark.asyncio
async def test_other_unit_cannot_chat_with_lamp(monkeypatch, client):
    monkeypatch.setattr(agent_mod, "get_agent", lambda: _FakeAgent())
    headers = await _login_admin(client)
    r = await client.post("/api/esp32/esp32_desk_sound/chat", json={"text": "hi"}, headers=headers)
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_chat_requires_authenticated_admin(monkeypatch, client):
    monkeypatch.setattr(agent_mod, "get_agent", lambda: _FakeAgent())
    r = await client.post("/api/esp32/x/chat", json={"text": "hi"})
    assert r.status_code == 401
