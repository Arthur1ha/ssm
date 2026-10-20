"""灯智能体 chat / memory HTTP 端点测试。"""
import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI

from cloud.esp32 import router as esp32_router
from cloud.esp32 import agent as agent_mod
from cloud.api.sessions import router as sessions_router


class _FakeAgent:
    _belief_summary = "傍晚常调暖光"

    def handle_user_text(self, text):
        return f"收到：{text}"


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


async def _login_admin(client):
    response = await client.post("/api/admin/session")
    assert response.status_code == 200
    return {"X-CSRF-Token": client.cookies.get("ssm_csrf")}


@pytest.mark.asyncio
async def test_chat_routes_to_agent(monkeypatch, client):
    monkeypatch.setattr(agent_mod, "get_agent", lambda: _FakeAgent())
    headers = await _login_admin(client)
    r = await client.post(
        "/api/esp32/esp32_desk_led/chat", json={"text": "你好"}, headers=headers
    )
    assert r.status_code == 200
    assert r.json() == {"reply": "收到：你好", "llm_available": True}


@pytest.mark.asyncio
async def test_memory_returns_taught(monkeypatch, tmp_path, client):
    from cloud.esp32.memory import taught
    monkeypatch.setattr(taught, "TAUGHT_FILE", tmp_path / "taught.json")
    taught.add("天黑了", "调亮", path=tmp_path / "taught.json")
    monkeypatch.setattr(agent_mod, "get_agent", lambda: _FakeAgent())

    await _login_admin(client)
    r = await client.get("/api/esp32/esp32_desk_led/memory")
    assert r.status_code == 200
    body = r.json()
    assert len(body["taught"]) == 1
    assert body["belief_summary"] == "傍晚常调暖光"


@pytest.mark.asyncio
async def test_chat_503_when_agent_missing(monkeypatch, client):
    monkeypatch.setattr(agent_mod, "get_agent", lambda: None)
    headers = await _login_admin(client)
    r = await client.post("/api/esp32/x/chat", json={"text": "hi"}, headers=headers)
    assert r.status_code == 503


@pytest.mark.asyncio
async def test_chat_requires_authenticated_admin(monkeypatch, client):
    monkeypatch.setattr(agent_mod, "get_agent", lambda: _FakeAgent())
    r = await client.post("/api/esp32/x/chat", json={"text": "hi"})
    assert r.status_code == 401
