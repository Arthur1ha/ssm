import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent))

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from cloud.go2.router import router
from cloud.go2 import connection as conn_module


@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


def test_status_disconnected(client):
    r = client.get("/api/go2/status")
    assert r.status_code == 200
    data = r.json()
    assert data["connected"] is False
    assert "state" in data


def test_command_503_when_not_connected(client):
    r = client.post("/api/go2/command", json={"cmd": "StandUp", "params": {}})
    assert r.status_code == 503


def test_command_400_for_unknown_cmd(client):
    conn_module.go2.is_connected = True
    conn_module.go2._conn = object()  # fake — ValueError raised before _conn is used
    try:
        r = client.post("/api/go2/command", json={"cmd": "FlyToMoon", "params": {}})
        assert r.status_code == 400
        assert "Unknown command" in r.json()["detail"]
    finally:
        conn_module.go2.is_connected = False
        conn_module.go2._conn = None


def test_video_503_when_not_connected(client):
    r = client.get("/api/go2/video")
    assert r.status_code == 503


def test_connect_500_when_env_missing(client, monkeypatch):
    monkeypatch.delenv("GO2_EMAIL", raising=False)
    monkeypatch.delenv("GO2_PASSWORD", raising=False)
    monkeypatch.delenv("GO2_SERIAL", raising=False)
    r = client.post("/api/go2/connect")
    assert r.status_code == 500
    assert "未配置" in r.json()["detail"]
