"""灯智能体 chat / memory HTTP 端点测试。"""
from fastapi import FastAPI
from fastapi.testclient import TestClient

from cloud.esp32 import router as esp32_router
from cloud.esp32 import agent as agent_mod


class _FakeAgent:
    _belief_summary = "傍晚常调暖光"

    def handle_user_text(self, text):
        return f"收到：{text}"


def _client():
    app = FastAPI()
    app.include_router(esp32_router.router)
    return TestClient(app)


def test_chat_routes_to_agent(monkeypatch):
    monkeypatch.setattr(agent_mod, "get_agent", lambda: _FakeAgent())
    r = _client().post("/api/esp32/esp32_desk_led/chat", json={"text": "你好"})
    assert r.status_code == 200
    assert r.json() == {"reply": "收到：你好"}


def test_memory_returns_taught(monkeypatch, tmp_path):
    from cloud.esp32.memory import taught
    monkeypatch.setattr(taught, "TAUGHT_FILE", tmp_path / "taught.json")
    taught.add("天黑了", "调亮", path=tmp_path / "taught.json")
    monkeypatch.setattr(agent_mod, "get_agent", lambda: _FakeAgent())

    r = _client().get("/api/esp32/esp32_desk_led/memory")
    assert r.status_code == 200
    body = r.json()
    assert len(body["taught"]) == 1
    assert body["belief_summary"] == "傍晚常调暖光"


def test_chat_503_when_agent_missing(monkeypatch):
    monkeypatch.setattr(agent_mod, "get_agent", lambda: None)
    r = _client().post("/api/esp32/x/chat", json={"text": "hi"})
    assert r.status_code == 503
