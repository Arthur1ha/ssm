# Go2 Air 浏览器控制 — 设计文档（修订版）

> 修订原因：原设计（浏览器直连 WebRTC）不可行。`unitree-webrtc-connect` 依赖 aiortc（Python），Unitree 云端信令协议需要 Python 层认证，浏览器无法独立完成 WebRTC 握手。改用 dimos 架构：Python 完整持有 WebRTC 连接，浏览器通过普通 HTTP 访问。

---

## 目标

让 PWA 浏览器通过 HTTP 控制 Unitree Go2 Air：查看视频流、切换模式、发送运动指令、查看实时状态。

---

## 架构

```
Go2 Air（嵌入式 Linux）
   ↕ WebRTC Remote（Unitree 云端信令 + TURN + aiortc 完整建立）
   ↕ unitree-webrtc-connect Python 库
Cloud Server（47.116.137.202）
   cloud/go2/connection.py   ← 单例，管理 WebRTC 生命周期
                                 - 视频帧：track.recv() → OpenCV → JPEG → 内存缓存
                                 - 状态订阅：DataChannel sub → asyncio.Queue → SSE listeners
                                 - 指令发送：datachannel.pub_sub.publish_request_new()
   cloud/go2/router.py       ← FastAPI APIRouter（挂载到现有 app）
       POST /api/go2/connect      ← 触发 WebRTC 连接（后台 asyncio task）
       POST /api/go2/disconnect   ← 断开连接
       GET  /api/go2/status       ← {"connected": bool, "state": {...}}
       GET  /api/go2/video        ← MJPEG 流（multipart/x-mixed-replace; boundary=frame）
       GET  /api/go2/state        ← SSE（text/event-stream），推送机器狗状态
       POST /api/go2/command      ← {"cmd": "StandUp"|"Move"|..., "params": {...}}
   cloud/api/main.py          ← include_router(go2_router)（无前缀）
   ↕ 普通 HTTP（过 ngrok 或直连）
Browser（PWA，app/src/pages/Go2Page.jsx）
   <img src="/api/go2/video">        ← MJPEG，<img> 标签即可
   EventSource("/api/go2/state")     ← SSE 实时状态
   POST /api/go2/command             ← fetch REST，发运动指令
```

**凭据安全**：GO2_EMAIL / GO2_PASSWORD 只存 `cloud/.env`，浏览器不持有。

---

## 模块设计

### cloud/go2/connection.py

单例类 `Go2Connection`，职责：

- `connect(email, password, serial, region)` → 创建 `UnitreeWebRTCConnection(Remote)` → `await conn.connect()` → 注册 video track callback + 订阅 `rt/lf/sportmodestate`
- video track callback：`async def on_track(track)` 循环 `await track.recv()` → `frame.to_ndarray(format="bgr24")` → `cv2.imencode('.jpg', ...)` → 存 `_latest_frame: bytes`
- `_on_state(msg)` 回调：解析状态 → `put_nowait` 到所有 SSE 队列
- `send_command(cmd, params)` → `pub_sub.publish_request_new(RTC_TOPIC["SPORT_MOD"], {"api_id": SPORT_CMD[cmd], "parameter": params})`
- `video_mjpeg_generator()` → async generator，每 33ms 读 `_latest_frame`，yield multipart 帧
- `new_state_queue() / remove_state_queue()` → SSE 监听者注册/注销

### cloud/go2/router.py

FastAPI `APIRouter`，5 个端点：

| 端点 | 实现要点 |
|------|---------|
| `POST /api/go2/connect` | `asyncio.create_task(go2.connect(...))` |
| `POST /api/go2/disconnect` | `await go2.disconnect()` |
| `GET /api/go2/status` | 直接返回 `{"connected": bool, "state": dict}` |
| `GET /api/go2/video` | `StreamingResponse(go2.video_mjpeg_generator(), media_type="multipart/x-mixed-replace; boundary=frame")` |
| `GET /api/go2/state` | `StreamingResponse(sse_generator(q), media_type="text/event-stream")`；SSE generator 从队列读，5s 超时发心跳 |
| `POST /api/go2/command` | `CommandRequest(cmd: str, params: dict = {})` → `await go2.send_command(cmd, params)` |

### cloud/api/main.py 变更

```python
from cloud.go2.router import router as go2_router
app.include_router(go2_router)
```

### app/src/pages/Go2Page.jsx

组件职责：
- 连接/断开按钮 → `POST /api/go2/connect` / `POST /api/go2/disconnect`
- `<img src="/api/go2/video">` 渲染 MJPEG（连接前隐藏）
- `EventSource("/api/go2/state")` → 更新状态面板（mode / body_height / velocity）
- 动作按钮：StandUp / StandDown / Hello / Stretch / Dance1 / Dance2 / StopMove
- 方向控制：前/后/左/右/左转/右转（pointerdown → 每 500ms 重复 Move，pointerup → StopMove）
- 连接状态轮询（GET /api/go2/status，每 3s）

---

## 命令参考（SPORT_CMD，已从库常量提取）

| 动作 | cmd 字符串 | params |
|------|-----------|--------|
| 站起 | `StandUp` | — |
| 坐下 | `StandDown` | — |
| 停止 | `StopMove` | — |
| 挥手 | `Hello` | — |
| 伸展 | `Stretch` | — |
| 舞蹈1 | `Dance1` | — |
| 舞蹈2 | `Dance2` | — |
| 移动 | `Move` | `{"x": float, "y": float, "z": float}` |

---

## 文件变更清单

| 操作 | 文件 | 说明 |
|------|------|------|
| 新建 | `cloud/go2/__init__.py` | 空文件 |
| 新建 | `cloud/go2/connection.py` | 单例 WebRTC 连接管理器 |
| 新建 | `cloud/go2/router.py` | 5 个 FastAPI 端点 |
| 修改 | `cloud/api/main.py` | include_router(go2_router) |
| 修改 | `cloud/.env` | 新增 GO2_EMAIL/PASSWORD/SERIAL/REGION |
| 新建 | `app/src/pages/Go2Page.jsx` | Go2 控制页（纯 HTTP） |
| 修改 | `app/index.html` | 加载 Go2Page.jsx（无需 SparkMD5） |
| 修改 | `app/src/app.jsx` | GO2_STATIC_DEVICE 注入 + go2-air 路由 |

---

## 不改动的部分

- `edge/`（ESP32 代码）
- `cloud/orchestrator/`（MQTT 编排器）
- MQTT 总线（Go2 控制路径完全不经过 MQTT）
- `protocol/topics.md`（本期无新 MQTT topic）

---

## 验收标准

1. `POST /api/go2/connect` 返回 200，WebRTC 连接建立（日志显示 🟢 connected）
2. `GET /api/go2/video` 在浏览器 `<img>` 中显示实时视频
3. `GET /api/go2/state` SSE 持续推送机器狗状态
4. 点击"站起"按钮，机器狗站起
5. 方向键持续按住，机器狗持续移动；松开停止

---

## 风险与注意事项

- **asyncio 事件循环**：aiortc 与 FastAPI/uvicorn 共享同一 asyncio loop，连接任务用 `asyncio.create_task()` 启动，不能用 `asyncio.run()`（会创建新 loop 导致冲突）
- **同时只能一个 WebRTC 客户端**：Go2 被占用时抛 `RobotBusyError`，API 返回 409
- **视频延迟**：MJPEG 经 Python OpenCV 转码，比原生 WebRTC 多 100-300ms，可接受
- **连接断开检测**：`iceConnectionState == "failed"` 时自动标记 `is_connected = False`，前端状态轮询会感知到
- **opencv-python**：已作为 `unitree-webrtc-connect` 的依赖自动安装，无需手动添加
