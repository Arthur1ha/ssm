import sys
import asyncio
import time
import uuid
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent))

import httpx
import pytest
import pytest_asyncio
from unittest.mock import AsyncMock
from unittest.mock import MagicMock
from fastapi import FastAPI

from cloud.go2.router import router
from cloud.go2 import connection as conn_module


@pytest_asyncio.fixture
async def client(monkeypatch):
    monkeypatch.setenv("SSM_INTERNAL_SERVICE_TOKEN", "test-service-token")
    from cloud.go2.state import Go2State
    monkeypatch.setattr(conn_module.go2, 'runtime', Go2State(conn_module.go2._state_changed))
    monkeypatch.setattr(conn_module.go2, '_sport_state_observed_at', time.monotonic(), raising=False)
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


@pytest.mark.asyncio
async def test_chat_task_stays_queryable_after_sync_window(client, monkeypatch):
    import cloud.agents.go2.graph as multitask
    finish = asyncio.Event()
    calls = []

    async def fake_run(*args, **kwargs):
        calls.append((args, kwargs))
        await finish.wait()
        return {"result": "ok", "response": "完成"}

    monkeypatch.setattr(multitask, "run_go2_loop", fake_run)
    task_id = f"test-{uuid.uuid4()}"
    payload = {"task_id": task_id, "message": "打招呼", "deadline": time.time() + 30, "sync_window_s": 0}
    sent = await client.post("/api/go2/chat", json=payload)
    assert sent.status_code == 202
    assert sent.json()["result"] == "executing"
    duplicate = await client.post("/api/go2/chat", json=payload)
    assert duplicate.status_code == 202
    assert len(calls) <= 1
    hidden = await client.get(f"/api/go2/tasks/{task_id}", headers={"X-SSM-Session-Id": "another-session"})
    assert hidden.status_code == 404
    finish.set()
    for _ in range(10):
        await asyncio.sleep(0)
        done = await client.get(f"/api/go2/tasks/{task_id}")
        if done.status_code == 200:
            break
    assert done.status_code == 200
    assert done.json()["response"] == "完成"
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_chat_task_cancel_confirms_stop(client, monkeypatch):
    import cloud.agents.go2.graph as multitask
    import cloud.go2.navigation.navigator as nav_mod
    blocker = asyncio.Event()

    async def fake_run(*args, **kwargs):
        await blocker.wait()
        return {"result": "ok"}

    monkeypatch.setattr(multitask, "run_go2_loop", fake_run)
    monkeypatch.setattr(nav_mod.navigator, "stop_and_confirm", AsyncMock(return_value={"result": "ok"}))
    from cloud.go2.control import control_lease
    from cloud.space.execution import ExecutionContext
    control_lease.acquire(ExecutionContext("test-admin", "admin", True))
    conn_module.go2.is_connected = True
    try:
        task_id = f"test-{uuid.uuid4()}"
        sent = await client.post("/api/go2/chat", json={"task_id": task_id, "message": "挥手", "sync_window_s": 0})
        assert sent.status_code == 202
        cancelled = await client.post(f"/api/go2/tasks/{task_id}/cancel")
        assert cancelled.status_code == 200
        assert cancelled.json()["result"] == "cancelled"
        nav_mod.navigator.stop_and_confirm.assert_awaited_once_with(owner_session_id="test-admin")
    finally:
        control_lease.release("test-admin")
        conn_module.go2.is_connected = False


@pytest.mark.asyncio
async def test_task_stop_never_sends_to_new_lease_owner(monkeypatch):
    from cloud.go2.control import control_lease
    from cloud.space.execution import ExecutionContext
    from cloud.go2.router import _stop_task_motion

    old = ExecutionContext("old-session", "visitor", False)
    new = ExecutionContext("new-session", "admin", True)
    from cloud.go2.navigation.navigator import navigator
    stop = AsyncMock(return_value={"result": "ok"})
    monkeypatch.setattr(navigator, "stop_and_confirm", stop)
    control_lease.release("test-admin")
    control_lease.release("old-session")
    control_lease.release("new-session")
    assert (await _stop_task_motion(old))["result"] == "ok"
    stop.assert_awaited_once_with(owner_session_id="old-session")
    stop.reset_mock()
    control_lease.acquire(new)
    try:
        result = await _stop_task_motion(old)
        assert result["result"] == "accepted"
        assert result["error"] == "control_lease_transferred"
        stop.assert_not_awaited()
    finally:
        control_lease.release("new-session")


@pytest.mark.asyncio
async def test_chat_deadline_reports_unconfirmed_stop(client, monkeypatch):
    import cloud.agents.go2.graph as multitask
    from cloud.go2.control import control_lease
    from cloud.space.execution import ExecutionContext

    async def pending_run(*args, **kwargs):
        await asyncio.Event().wait()

    monkeypatch.setattr(multitask, "run_go2_loop", pending_run)
    from cloud.go2.navigation.navigator import navigator
    stop = AsyncMock(return_value={"result": "accepted", "error": "stop_unconfirmed"})
    monkeypatch.setattr(navigator, "stop_and_confirm", stop)
    control_lease.acquire(ExecutionContext("test-admin", "admin", True))
    conn_module.go2.is_connected = True
    try:
        task_id = f"test-{uuid.uuid4()}"
        sent = await client.post("/api/go2/chat", json={
            "task_id": task_id, "message": "移动", "deadline": time.time() + 0.1,
            "sync_window_s": 1,
        })
        assert sent.status_code == 200
        assert sent.json()["result"] == "accepted"
        assert sent.json()["error"] == "stop_unconfirmed"
        stop.assert_awaited_once_with(owner_session_id="test-admin")
    finally:
        control_lease.release("test-admin")
        conn_module.go2.is_connected = False
