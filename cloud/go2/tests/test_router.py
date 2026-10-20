import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent))

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI

from cloud.go2.router import router
from cloud.go2 import connection as conn_module


@pytest_asyncio.fixture
async def client(monkeypatch):
    monkeypatch.setenv("SSM_INTERNAL_SERVICE_TOKEN", "test-service-token")
    app = FastAPI()
    app.include_router(router)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
        headers={
            "X-SSM-Service-Token": "test-service-token",
            "X-SSM-Session-Id": "test-admin",
            "X-SSM-Actor": "admin",
            "X-SSM-Persist": "true",
            "X-SSM-Space-Id": "main",
        },
    ) as value:
        yield value


@pytest.mark.asyncio
async def test_status_disconnected(client):
    r = await client.get("/api/go2/connection")
    assert r.status_code == 200
    data = r.json()
    assert data["connected"] is False
    assert "state" in data


@pytest.mark.asyncio
async def test_command_503_when_not_connected(client):
    r = await client.post("/api/go2/commands", json={"cmd": "StandUp", "params": {}})
    assert r.status_code == 503


@pytest.mark.asyncio
async def test_command_400_for_unknown_cmd(client):
    conn_module.go2.is_connected = True
    conn_module.go2._conn = object()  # fake — ValueError raised before _conn is used
    try:
        r = await client.post("/api/go2/commands", json={"cmd": "FlyToMoon", "params": {}})
        assert r.status_code == 400
        assert "Unknown command" in r.json()["detail"]
    finally:
        conn_module.go2.is_connected = False
        conn_module.go2._conn = None


@pytest.mark.asyncio
async def test_video_503_when_not_connected(client):
    r = await client.get("/api/go2/video")
    assert r.status_code == 503


@pytest.mark.asyncio
async def test_connect_500_when_env_missing(client, monkeypatch):
    monkeypatch.delenv("GO2_EMAIL", raising=False)
    monkeypatch.delenv("GO2_PASSWORD", raising=False)
    monkeypatch.delenv("GO2_SERIAL", raising=False)
    r = await client.post("/api/go2/connection")
    assert r.status_code == 500
    assert "未配置" in r.json()["detail"]


@pytest.mark.asyncio
async def test_tag_location_503_when_not_connected(client):
    r = await client.post("/api/go2/navigation/locations", json={"name": "门口"})
    assert r.status_code == 503


@pytest.mark.asyncio
async def test_list_locations_returns_list(client):
    r = await client.get("/api/go2/navigation/locations")
    assert r.status_code == 200
    assert isinstance(r.json(), list)


@pytest.mark.asyncio
async def test_nav_state_returns_mode(client):
    r = await client.get("/api/go2/navigation/state")
    assert r.status_code == 200
    assert "mode" in r.json()


@pytest.mark.asyncio
async def test_led_400_unknown_color(client):
    import cloud.go2.connection as conn_module
    conn_module.go2.is_connected = True
    try:
        r = await client.put("/api/go2/led", json={"color": "pink", "duration": 30})
        assert r.status_code == 400
    finally:
        conn_module.go2.is_connected = False


def test_go2_mode_setter_已删除():
    """死代码 PUT /api/go2/mode 已删除（切 normal 会让狗忽略遥控，是踩坑端点）。"""
    from cloud.go2.router import router
    paths = {(r.path, m) for r in router.routes for m in getattr(r, "methods", set())}
    assert ("/api/go2/mode", "PUT") not in paths
