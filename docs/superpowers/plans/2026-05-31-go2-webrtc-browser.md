# Go2 Air 浏览器控制 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让 PWA 浏览器通过 HTTP 接口控制 Unitree Go2 Air，Python 进程持有完整 WebRTC 连接，浏览器通过 MJPEG/SSE/REST 访问。

**Architecture:** Python `cloud/go2/connection.py` 单例持有 `UnitreeWebRTCConnection(Remote)`，视频帧经 OpenCV 转为 JPEG 存入内存，`cloud/go2/router.py` 将其暴露为 MJPEG 流 + SSE 状态 + REST 指令，挂载到现有 FastAPI app。浏览器 `Go2Page.jsx` 用 `<img>` + `EventSource` + `fetch` 消费，无任何 WebRTC 代码。

**Tech Stack:** unitree-webrtc-connect (已安装, v2.1.2), aiortc (已安装, v1.14.0), opencv-python (已安装，unitree-webrtc-connect 依赖), FastAPI StreamingResponse, React 18 (Babel standalone, 无构建)

---

## 文件结构

| 操作 | 文件 | 职责 |
|------|------|------|
| 新建 | `cloud/go2/__init__.py` | 空，使 go2 成为 Python 包 |
| 新建 | `cloud/go2/connection.py` | Go2Connection 单例：WebRTC 连接、帧缓存、状态订阅、指令发送 |
| 新建 | `cloud/go2/router.py` | FastAPI APIRouter：5 个端点 |
| 新建 | `cloud/go2/tests/__init__.py` | 空 |
| 新建 | `cloud/go2/tests/test_connection.py` | 单元测试：连接状态校验、指令校验 |
| 新建 | `cloud/go2/tests/test_router.py` | 集成测试：API 端点无需真实 Go2 |
| 修改 | `cloud/api/main.py` | 末尾 include_router(go2_router) |
| 修改 | `cloud/.env` | 新增 GO2_EMAIL/PASSWORD/SERIAL/REGION |
| 新建 | `app/src/pages/Go2Page.jsx` | Go2 控制页面（纯 HTTP） |
| 修改 | `app/index.html` | Go2Page.jsx script 标签 |
| 修改 | `app/src/app.jsx` | GO2_STATIC_DEVICE + go2-air 路由 |

---

## Task 1: Go2Connection 单例（connection.py）

**Files:**
- Create: `cloud/go2/__init__.py`
- Create: `cloud/go2/connection.py`
- Create: `cloud/go2/tests/__init__.py`
- Create: `cloud/go2/tests/test_connection.py`

- [ ] **Step 1: 创建包目录和空文件**

```bash
mkdir -p /home/eliott/ssm/cloud/go2/tests
touch /home/eliott/ssm/cloud/go2/__init__.py
touch /home/eliott/ssm/cloud/go2/tests/__init__.py
```

- [ ] **Step 2: 先写失败测试**

写入 `/home/eliott/ssm/cloud/go2/tests/test_connection.py`：

```python
import asyncio
import pytest
from unittest.mock import MagicMock, AsyncMock, patch

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent))

from cloud.go2.connection import Go2Connection


def test_initial_state():
    conn = Go2Connection()
    assert conn.is_connected is False
    assert conn._latest_frame is None
    assert conn._robot_state == {}


def test_send_command_raises_when_not_connected():
    conn = Go2Connection()
    with pytest.raises(RuntimeError, match="not connected"):
        asyncio.run(conn.send_command("StandUp"))


def test_send_command_raises_for_unknown_command():
    conn = Go2Connection()
    conn.is_connected = True
    conn._conn = MagicMock()
    # ValueError must be raised before _conn is accessed
    with pytest.raises(ValueError, match="Unknown command"):
        asyncio.run(conn.send_command("FlyToMoon"))


def test_on_state_updates_robot_state_and_notifies_queues():
    conn = Go2Connection()
    q = asyncio.Queue(maxsize=10)
    conn._state_queues.append(q)

    msg = {"data": {"data": {"mode": 1, "body_height": 0.32, "velocity": [0.1, 0.0, 0.0]}}}
    conn._on_state(msg)

    assert conn._robot_state["mode"] == 1
    assert conn._robot_state["body_height"] == 0.32
    assert q.qsize() == 1


def test_new_and_remove_state_queue():
    conn = Go2Connection()
    q = conn.new_state_queue()
    assert q in conn._state_queues
    conn.remove_state_queue(q)
    assert q not in conn._state_queues


def test_remove_nonexistent_queue_does_not_raise():
    conn = Go2Connection()
    q = asyncio.Queue()
    conn.remove_state_queue(q)  # should not raise
```

- [ ] **Step 3: 运行测试，确认失败（模块不存在）**

```bash
cd /home/eliott/ssm
uv run pytest cloud/go2/tests/test_connection.py -v 2>&1 | head -20
```

期望输出：`ModuleNotFoundError: No module named 'cloud.go2.connection'`

- [ ] **Step 4: 写入 connection.py 实现**

写入 `/home/eliott/ssm/cloud/go2/connection.py`：

```python
import asyncio
import logging

import cv2

from unitree_webrtc_connect import UnitreeWebRTCConnection, WebRTCConnectionMethod
from unitree_webrtc_connect.constants import RTC_TOPIC, SPORT_CMD


class Go2Connection:
    def __init__(self) -> None:
        self._conn: UnitreeWebRTCConnection | None = None
        self.is_connected: bool = False
        self._latest_frame: bytes | None = None
        self._robot_state: dict = {}
        self._state_queues: list[asyncio.Queue] = []

    async def connect(self, email: str, password: str, serial: str, region: str = "cn") -> None:
        """建立 Remote 模式 WebRTC 连接。应作为 asyncio.create_task() 调用。"""
        if self._conn:
            await self._conn.disconnect()

        self._conn = UnitreeWebRTCConnection(
            WebRTCConnectionMethod.Remote,
            serialNumber=serial,
            username=email,
            password=password,
            region=region,
            device_type="Go2",
        )

        async def on_track(track):
            while True:
                try:
                    frame = await track.recv()
                    arr = frame.to_ndarray(format="bgr24")
                    _, buf = cv2.imencode(".jpg", arr, [cv2.IMWRITE_JPEG_QUALITY, 75])
                    self._latest_frame = buf.tobytes()
                except Exception:
                    break

        self._conn.video.add_track_callback(on_track)

        try:
            await self._conn.connect()
        except Exception as exc:
            self.is_connected = False
            logging.error("[Go2] 连接失败: %s", exc)
            raise

        self._conn.datachannel.pub_sub.subscribe(
            RTC_TOPIC["LF_SPORT_MOD_STATE"], self._on_state
        )
        self.is_connected = True
        logging.info("[Go2] WebRTC 连接成功")

    async def disconnect(self) -> None:
        self.is_connected = False
        if self._conn:
            await self._conn.disconnect()
            self._conn = None
        logging.info("[Go2] 已断开")

    def _on_state(self, msg: dict) -> None:
        data = msg.get("data", {})
        inner = data.get("data", data) if isinstance(data, dict) else {}
        self._robot_state = {
            "mode":        inner.get("mode"),
            "body_height": inner.get("body_height"),
            "velocity":    inner.get("velocity"),
        }
        for q in self._state_queues:
            try:
                q.put_nowait(self._robot_state.copy())
            except asyncio.QueueFull:
                pass

    async def send_command(self, cmd: str, params: dict | None = None) -> None:
        if not self.is_connected or not self._conn:
            raise RuntimeError("Go2 not connected")
        api_id = SPORT_CMD.get(cmd)
        if api_id is None:
            raise ValueError(f"Unknown command: {cmd}")
        options: dict = {"api_id": api_id}
        if params:
            options["parameter"] = params
        await self._conn.datachannel.pub_sub.publish_request_new(
            RTC_TOPIC["SPORT_MOD"], options
        )

    async def mjpeg_generator(self):
        """Async generator，每 ~33ms yield 一帧 MJPEG multipart 数据。"""
        while self.is_connected:
            if self._latest_frame:
                yield (
                    b"--frame\r\nContent-Type: image/jpeg\r\n\r\n"
                    + self._latest_frame
                    + b"\r\n"
                )
            await asyncio.sleep(0.033)

    def new_state_queue(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=20)
        self._state_queues.append(q)
        return q

    def remove_state_queue(self, q: asyncio.Queue) -> None:
        try:
            self._state_queues.remove(q)
        except ValueError:
            pass


go2 = Go2Connection()
```

- [ ] **Step 5: 运行测试，确认全部通过**

```bash
cd /home/eliott/ssm
uv run pytest cloud/go2/tests/test_connection.py -v
```

期望输出：
```
PASSED test_initial_state
PASSED test_send_command_raises_when_not_connected
PASSED test_send_command_raises_for_unknown_command
PASSED test_on_state_updates_robot_state_and_notifies_queues
PASSED test_new_and_remove_state_queue
PASSED test_remove_nonexistent_queue_does_not_raise
6 passed
```

- [ ] **Step 6: Commit**

```bash
git add cloud/go2/__init__.py cloud/go2/connection.py cloud/go2/tests/__init__.py cloud/go2/tests/test_connection.py
git commit -m "feat: Go2Connection 单例 — WebRTC 连接管理、帧缓存、状态订阅、指令发送"
```

---

## Task 2: Go2 FastAPI 路由（router.py）

**Files:**
- Create: `cloud/go2/router.py`
- Create: `cloud/go2/tests/test_router.py`

- [ ] **Step 1: 先写失败测试**

写入 `/home/eliott/ssm/cloud/go2/tests/test_router.py`：

```python
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
```

- [ ] **Step 2: 运行测试，确认失败**

```bash
cd /home/eliott/ssm
uv run pytest cloud/go2/tests/test_router.py -v 2>&1 | head -20
```

期望：`ModuleNotFoundError: No module named 'cloud.go2.router'`

- [ ] **Step 3: 写入 router.py 实现**

写入 `/home/eliott/ssm/cloud/go2/router.py`：

```python
import asyncio
import json
import os

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from cloud.go2.connection import go2

router = APIRouter()


@router.post("/api/go2/connect")
async def go2_connect():
    email    = os.getenv("GO2_EMAIL", "")
    password = os.getenv("GO2_PASSWORD", "")
    serial   = os.getenv("GO2_SERIAL", "")
    region   = os.getenv("GO2_REGION", "cn")
    if not email or not password or not serial:
        raise HTTPException(status_code=500, detail="GO2_EMAIL/PASSWORD/SERIAL 未配置")
    asyncio.create_task(go2.connect(email, password, serial, region))
    return {"status": "connecting"}


@router.post("/api/go2/disconnect")
async def go2_disconnect():
    await go2.disconnect()
    return {"status": "disconnected"}


@router.get("/api/go2/status")
def go2_status():
    return {"connected": go2.is_connected, "state": go2._robot_state}


@router.get("/api/go2/video")
async def go2_video():
    if not go2.is_connected:
        raise HTTPException(status_code=503, detail="Go2 not connected")
    return StreamingResponse(
        go2.mjpeg_generator(),
        media_type="multipart/x-mixed-replace; boundary=frame",
    )


@router.get("/api/go2/state")
async def go2_state():
    async def sse_gen():
        q = go2.new_state_queue()
        try:
            while go2.is_connected:
                try:
                    state = await asyncio.wait_for(q.get(), timeout=5.0)
                    yield f"data: {json.dumps(state)}\n\n"
                except asyncio.TimeoutError:
                    yield "data: {}\n\n"
        finally:
            go2.remove_state_queue(q)

    return StreamingResponse(sse_gen(), media_type="text/event-stream")


class CommandRequest(BaseModel):
    cmd: str
    params: dict = {}


@router.post("/api/go2/command")
async def go2_command(req: CommandRequest):
    try:
        await go2.send_command(req.cmd, req.params or None)
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"ok": True}
```

- [ ] **Step 4: 运行测试，确认全部通过**

```bash
cd /home/eliott/ssm
uv run pytest cloud/go2/tests/test_router.py -v
```

期望输出：
```
PASSED test_status_disconnected
PASSED test_command_503_when_not_connected
PASSED test_command_400_for_unknown_cmd
PASSED test_video_503_when_not_connected
PASSED test_connect_500_when_env_missing
5 passed
```

- [ ] **Step 5: Commit**

```bash
git add cloud/go2/router.py cloud/go2/tests/test_router.py
git commit -m "feat: Go2 FastAPI 路由 — MJPEG/SSE/REST 5 个端点"
```

---

## Task 3: 挂载路由到现有 FastAPI App

**Files:**
- Modify: `cloud/api/main.py`

- [ ] **Step 1: 在 cloud/api/main.py 顶部 import 区末尾添加**

在 `from openai import OpenAI` 这行后面插入：

```python
from cloud.go2.router import router as go2_router
```

- [ ] **Step 2: 在 `app = FastAPI()` 之后、`app.add_middleware` 之前，挂载路由**

找到这两行：
```python
app = FastAPI()
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])
```

在 `app = FastAPI()` 之后插入：
```python
app.include_router(go2_router)
```

完整结果：
```python
app = FastAPI()
app.include_router(go2_router)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])
```

- [ ] **Step 3: 重启 API，验证端点注册成功**

```bash
cd /home/eliott/ssm
make restart-api
sleep 4
curl -s http://127.0.0.1:8082/openapi.json | python3 -c "
import sys, json
paths = json.load(sys.stdin)['paths']
for p in sorted(paths):
    if 'go2' in p:
        print(p)
"
```

期望输出：
```
/api/go2/command
/api/go2/connect
/api/go2/disconnect
/api/go2/state
/api/go2/status
/api/go2/video
```

- [ ] **Step 4: 验证 status 端点**

```bash
curl -s http://127.0.0.1:8082/api/go2/status
```

期望：`{"connected":false,"state":{}}`

- [ ] **Step 5: Commit**

```bash
git add cloud/api/main.py
git commit -m "feat: 将 Go2 路由挂载到主 FastAPI app"
```

---

## Task 4: 配置凭据

**Files:**
- Modify: `cloud/.env`

- [ ] **Step 1: 在 cloud/.env 末尾追加**

```
# Unitree Go2 Air
GO2_EMAIL=18391362895
GO2_PASSWORD=Awf020906
GO2_SERIAL=B42D1000Q3JE0J8D
GO2_REGION=cn
```

（凭据与 test_script/test_remote_connection.py 中一致）

- [ ] **Step 2: 重启 API，验证凭据已加载（不再 500）**

```bash
make restart-api
sleep 4
curl -s -X POST http://127.0.0.1:8082/api/go2/connect
```

期望：`{"status":"connecting"}`（不是 "未配置" 错误）

- [ ] **Step 3: Commit**

```bash
git add cloud/.env
git commit -m "feat: 新增 Go2 Air 凭据到 cloud/.env"
```

---

## Task 5: 前端 Go2Page.jsx

**Files:**
- Create: `app/src/pages/Go2Page.jsx`
- Modify: `app/index.html`

- [ ] **Step 1: 创建 pages 目录**

```bash
mkdir -p /home/eliott/ssm/app/src/pages
```

- [ ] **Step 2: 写入 Go2Page.jsx**

写入 `/home/eliott/ssm/app/src/pages/Go2Page.jsx`：

```jsx
/* Go2DevicePage — Unitree Go2 Air 控制页面
   通信：MJPEG (<img>) + SSE (EventSource) + REST (fetch)
   无 WebRTC 代码，无外部依赖。
*/
function Go2DevicePage({ onBack }) {
  const { useState, useEffect, useRef, useCallback } = React;

  const [status, setStatus]    = useState("idle"); // idle|connecting|connected|error
  const [robotState, setRobot] = useState(null);
  const [error, setError]      = useState("");
  const [moving, setMoving]    = useState(null);
  const moveIntervalRef        = useRef(null);
  const esRef                  = useRef(null);
  const pollRef                = useRef(null);

  const LIME = "#C8FF3E";

  // ── 状态轮询（每 3s，检测连接状态变化）───────────────────────────
  const pollStatus = useCallback(async () => {
    try {
      const r = await fetch("/api/go2/status");
      const data = await r.json();
      if (data.connected && status !== "connected") {
        setStatus("connected");
      } else if (!data.connected && status === "connected") {
        setStatus("idle");
        setRobot(null);
      }
    } catch (_) {}
  }, [status]);

  useEffect(() => {
    pollRef.current = setInterval(pollStatus, 3000);
    return () => clearInterval(pollRef.current);
  }, [pollStatus]);

  // ── SSE 状态订阅（已连接时启动）─────────────────────────────────
  useEffect(() => {
    if (status !== "connected") return;
    const es = new EventSource("/api/go2/state");
    esRef.current = es;
    es.onmessage = (e) => {
      try {
        const d = JSON.parse(e.data);
        if (d.mode !== undefined) setRobot(d);
      } catch (_) {}
    };
    return () => { es.close(); esRef.current = null; };
  }, [status]);

  // ── 清理 ─────────────────────────────────────────────────────────
  useEffect(() => () => {
    if (moveIntervalRef.current) clearInterval(moveIntervalRef.current);
    if (esRef.current) esRef.current.close();
  }, []);

  // ── 连接 / 断开 ──────────────────────────────────────────────────
  const connect = async () => {
    setError("");
    setStatus("connecting");
    try {
      const r = await fetch("/api/go2/connect", { method: "POST" });
      if (!r.ok) throw new Error((await r.json()).detail || await r.text());
    } catch (e) {
      setStatus("error");
      setError(String(e));
    }
  };

  const disconnect = async () => {
    if (moveIntervalRef.current) { clearInterval(moveIntervalRef.current); moveIntervalRef.current = null; }
    if (esRef.current) { esRef.current.close(); esRef.current = null; }
    await fetch("/api/go2/disconnect", { method: "POST" });
    setStatus("idle");
    setRobot(null);
    setMoving(null);
  };

  // ── 指令发送 ─────────────────────────────────────────────────────
  const sendCmd = (cmd, params) => {
    fetch("/api/go2/command", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ cmd, params: params || {} }),
    });
  };

  // ── 持续移动 ─────────────────────────────────────────────────────
  const startMove = (x, y, z, label) => {
    if (moveIntervalRef.current) clearInterval(moveIntervalRef.current);
    setMoving(label);
    const fire = () => sendCmd("Move", { x, y, z });
    fire();
    moveIntervalRef.current = setInterval(fire, 500);
  };

  const stopMove = () => {
    if (moveIntervalRef.current) { clearInterval(moveIntervalRef.current); moveIntervalRef.current = null; }
    setMoving(null);
    sendCmd("StopMove");
  };

  // ── UI ────────────────────────────────────────────────────────────
  const connected = status === "connected";
  const busy      = status === "connecting";

  const statusLabel = { idle: "未连接", connecting: "连接中…", connected: "已连接 ✓", error: "连接失败" }[status];
  const statusColor = { idle: "#666", connecting: "#f0a500", connected: LIME, error: "#ff4444" }[status];

  const btn = (color = "#2a2a2a", disabled = false) => ({
    background: disabled ? "#222" : color,
    color: disabled ? "#555" : color === LIME ? "#000" : "#fff",
    border: "none", borderRadius: 8, padding: "10px 16px",
    fontWeight: 600, fontSize: 14, cursor: disabled ? "not-allowed" : "pointer",
    WebkitTapHighlightColor: "transparent",
  });

  return (
    <div style={{ background: "#0B0B0E", color: "#fff", minHeight: "100vh", fontFamily: "system-ui, sans-serif" }}>

      {/* Header */}
      <div style={{ display: "flex", alignItems: "center", gap: 12, padding: "16px 20px", borderBottom: "1px solid #1a1a1a" }}>
        <button onClick={onBack} style={{ background: "none", border: "none", color: "#888", cursor: "pointer", fontSize: 20, padding: 0 }}>←</button>
        <span style={{ fontSize: 17, fontWeight: 700 }}>Go2 Air</span>
        <span style={{ marginLeft: "auto", fontSize: 13, color: statusColor, fontWeight: 600 }}>{statusLabel}</span>
      </div>

      <div style={{ padding: "16px 20px", display: "flex", flexDirection: "column", gap: 14 }}>

        {/* 连接 / 断开 */}
        {!connected
          ? <button onClick={connect} disabled={busy} style={btn(LIME, busy)}>{busy ? "连接中…" : "连接 Go2"}</button>
          : <button onClick={disconnect} style={btn("#ff4444")}>断开连接</button>
        }

        {error && (
          <div style={{ background: "#1a0000", border: "1px solid #ff4444", borderRadius: 8, padding: "10px 14px", fontSize: 13, color: "#ff6666" }}>
            {error}
          </div>
        )}

        {/* 视频流 */}
        <div style={{ borderRadius: 12, overflow: "hidden", background: "#000", aspectRatio: "16/9", display: "flex", alignItems: "center", justifyContent: "center" }}>
          {connected
            ? <img src="/api/go2/video" alt="Go2 视频流"
                style={{ width: "100%", height: "100%", objectFit: "cover", display: "block" }} />
            : <span style={{ color: "#333", fontSize: 13 }}>视频流（连接后显示）</span>
          }
        </div>

        {/* 状态面板 */}
        {robotState && (
          <div style={{ background: "#141414", borderRadius: 12, padding: "12px 16px", display: "grid", gridTemplateColumns: "1fr 1fr 1fr", gap: 8 }}>
            {[
              { label: "模式", value: robotState.mode ?? "—" },
              { label: "体高 m", value: typeof robotState.body_height === "number" ? robotState.body_height.toFixed(3) : "—" },
              { label: "速度", value: Array.isArray(robotState.velocity) ? robotState.velocity[0]?.toFixed(2) : "—" },
            ].map(({ label, value }) => (
              <div key={label} style={{ textAlign: "center" }}>
                <div style={{ fontSize: 11, color: "#555", marginBottom: 3 }}>{label}</div>
                <div style={{ fontSize: 16, fontWeight: 700, color: LIME }}>{value}</div>
              </div>
            ))}
          </div>
        )}

        {/* 动作按钮 */}
        <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 8 }}>
          {[
            ["站起",  "StandUp"],
            ["坐下",  "StandDown"],
            ["挥手",  "Hello"],
            ["伸展",  "Stretch"],
            ["舞蹈1", "Dance1"],
            ["舞蹈2", "Dance2"],
          ].map(([label, cmd]) => (
            <button key={cmd} disabled={!connected} onClick={() => sendCmd(cmd)} style={btn("#1e1e1e", !connected)}>
              {label}
            </button>
          ))}
        </div>

        {/* 移动控制 */}
        <div style={{ background: "#141414", borderRadius: 12, padding: 16 }}>
          <div style={{ fontSize: 12, color: "#555", textAlign: "center", marginBottom: 10 }}>
            移动控制{moving && <span style={{ color: LIME }}>（{moving}）</span>}
          </div>
          <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr 1fr", gap: 8, maxWidth: 180, margin: "0 auto" }}>
            <div />
            <button disabled={!connected} style={btn("#1e1e1e", !connected)}
              onPointerDown={() => startMove(0.3, 0, 0, "前进")} onPointerUp={stopMove} onPointerLeave={stopMove}>▲</button>
            <div />
            <button disabled={!connected} style={btn("#1e1e1e", !connected)}
              onPointerDown={() => startMove(0, 0.3, 0, "左移")} onPointerUp={stopMove} onPointerLeave={stopMove}>◀</button>
            <button disabled={!connected} style={btn("#ff4444", !connected)} onClick={stopMove}>■</button>
            <button disabled={!connected} style={btn("#1e1e1e", !connected)}
              onPointerDown={() => startMove(0, -0.3, 0, "右移")} onPointerUp={stopMove} onPointerLeave={stopMove}>▶</button>
            <div />
            <button disabled={!connected} style={btn("#1e1e1e", !connected)}
              onPointerDown={() => startMove(-0.3, 0, 0, "后退")} onPointerUp={stopMove} onPointerLeave={stopMove}>▼</button>
            <div />
          </div>
          <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 8, maxWidth: 180, margin: "10px auto 0" }}>
            <button disabled={!connected} style={btn("#1e1e1e", !connected)}
              onPointerDown={() => startMove(0, 0, 0.5, "左转")} onPointerUp={stopMove} onPointerLeave={stopMove}>↺ 左转</button>
            <button disabled={!connected} style={btn("#1e1e1e", !connected)}
              onPointerDown={() => startMove(0, 0, -0.5, "右转")} onPointerUp={stopMove} onPointerLeave={stopMove}>↻ 右转</button>
          </div>
        </div>

      </div>
    </div>
  );
}
```

- [ ] **Step 3: 在 app/index.html 的 `<script type="text/babel" src="src/app.jsx">` 之前插入 Go2Page 脚本**

找到这一行：
```html
  <script type="text/babel" src="src/app.jsx"></script>
```

在其之前插入（Go2Page.jsx 必须先于 app.jsx 加载，因为 app.jsx 会引用 `Go2DevicePage`）：
```html
  <script type="text/babel" src="src/pages/Go2Page.jsx"></script>
```

完整末尾区域如下：
```html
  <script src="src/MqttBus.js"></script>
  <script src="src/AgentRegistry.js"></script>
  <script src="src/ISMTracker.js"></script>

  <div id="root"></div>

  <script type="text/babel" src="src/pages/Go2Page.jsx"></script>
  <script type="text/babel" src="src/app.jsx"></script>
```

- [ ] **Step 4: 验证文件存在**

```bash
ls -lh /home/eliott/ssm/app/src/pages/Go2Page.jsx
```

期望：文件存在，大小 > 4KB

- [ ] **Step 5: Commit**

```bash
git add app/src/pages/Go2Page.jsx app/index.html
git commit -m "feat: Go2DevicePage 控制页面 — MJPEG 视频 + SSE 状态 + REST 指令"
```

---

## Task 6: 更新 app.jsx — 注入 Go2 设备 + 路由

**Files:**
- Modify: `app/src/app.jsx`

- [ ] **Step 1: 在 function App() 定义之前（约 line 1288）插入 Go2 静态设备常量**

找到这一行（约 line 1288）：
```javascript
function App() {
```

在其之前插入：
```javascript
const GO2_STATIC_DEVICE = {
  unit_id:      "go2_main",
  agent_id:     "go2_main",
  slug:         "go2-air",
  name:         "Go2 Air",
  agent_type:   "robot",
  capabilities: ["MOVE", "STAND_UP", "SIT_DOWN", "HELLO", "STRETCH", "DANCE"],
};

```

- [ ] **Step 2: 修改 useState 初始值，让 Go2 在页面加载时就出现在列表**

找到（约 line 1292 变为约 line 1298 加了 7 行后）：
```javascript
  const [agents, setAgents]               = useState([]);
```

改为：
```javascript
  const [agents, setAgents]               = useState([GO2_STATIC_DEVICE]);
```

- [ ] **Step 3: 修改 MQTT registry change 回调，确保 Go2 在 MQTT 事件后仍然存在**

找到（约 line 1362）：
```javascript
    registry.addEventListener('change', () => {
      setAgents(registry.getAll().filter(a =>
        a.agent_type && !EXCL_TYPES.has(a.agent_type) && !EXCL_PLAT.has(a.hw_platform) && a._online === true
      ));
    });
```

改为：
```javascript
    registry.addEventListener('change', () => {
      const mqttAgents = registry.getAll().filter(a =>
        a.agent_type && !EXCL_TYPES.has(a.agent_type) && !EXCL_PLAT.has(a.hw_platform) && a._online === true
      );
      setAgents([GO2_STATIC_DEVICE, ...mqttAgents]);
    });
```

- [ ] **Step 4: 在 hash 路由分发中，在通用设备路由之前插入 go2-air 专用路由**

找到（约 line 1417）：
```javascript
  const hashMatch = currentHash.match(/^#\/devices\/([^/]+)$/);
  if (hashMatch) {
    const slug   = hashMatch[1];
    const device = agents.find(a => a.slug === slug || (a.unit_id || a.agent_id) === slug);
```

改为：
```javascript
  const hashMatch = currentHash.match(/^#\/devices\/([^/]+)$/);
  if (hashMatch) {
    const slug = hashMatch[1];
    if (slug === "go2-air") {
      return <Go2DevicePage onBack={() => navigate('#')} />;
    }
    const device = agents.find(a => a.slug === slug || (a.unit_id || a.agent_id) === slug);
```

- [ ] **Step 5: 验证前端加载正常（没有 JS 语法错误）**

```bash
make pwa-bg
sleep 2
curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:8081/
```

期望：`200`

- [ ] **Step 6: Commit**

```bash
git add app/src/app.jsx
git commit -m "feat: app.jsx 注入 Go2 静态设备 + #/devices/go2-air 路由"
```

---

## Task 7: 端对端验证

- [ ] **Step 1: 运行全部单元测试**

```bash
cd /home/eliott/ssm
uv run pytest cloud/go2/tests/ -v
```

期望：11 passed，0 failed

- [ ] **Step 2: 确保所有服务运行**

```bash
make restart-api
sleep 4
make pwa-bg
sleep 2
make ps
```

期望看到：`uvicorn cloud.api.main` 和 `http.server 8081` 进程

- [ ] **Step 3: 打开 Go2 页面**

```bash
make ngrok-url
```

用输出的 URL 加 `#/devices/go2-air`，例如：
`https://xxx.ngrok-free.app/#/devices/go2-air`

在设备列表（Devices 标签页）也应能看到 "Go2 Air" 条目。

- [ ] **Step 4: 测试连接流程**

1. 点击"连接 Go2"
2. 检查 API 日志：`tail -f logs/api.log`（应看到 `🟡 started` → `🟢 connected`）
3. 状态变为"已连接 ✓"
4. 视频流出现在 `<img>` 区域

如果 Go2 不在线或凭据错误，logs/api.log 会显示具体错误。

- [ ] **Step 5: 测试指令**

点击"站起"按钮，`logs/api.log` 应出现对应的 DataChannel 发送日志，机器狗站起。

- [ ] **Step 6: 测试方向控制**

长按"▲"按钮，机器狗持续前进；松开后停止。

- [ ] **Step 7: 测试断开重连**

点击"断开连接" → 再次点击"连接 Go2"，应能正常重连。

---

## 常见问题排查

| 现象 | 原因 | 解决 |
|------|------|------|
| `/api/go2/connect` 返回 500 "未配置" | cloud/.env 没有 GO2_* 变量或 API 未重启 | `make restart-api` |
| `logs/api.log` 显示 `RobotBusyError` | Go2 被另一个 WebRTC 客户端（手机 App）占用 | 关闭宇树 App，再重连 |
| `logs/api.log` 显示 `NoSdpAnswerError` | Go2 未开机或未连网 | 检查机器狗状态 |
| 视频 `<img>` 区域空白 | WebRTC 视频 track 尚未建立（连接初期），等待 5-10s | 等待 |
| 状态面板不更新 | SSE 连接断开 | 刷新页面重新建立 SSE |
| 方向键松开后狗不停 | `onPointerLeave` 未触发（移出元素） | 已加 `onPointerLeave={stopMove}` 兜底 |
