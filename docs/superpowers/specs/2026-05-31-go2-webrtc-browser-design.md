# Go2 Air 浏览器直连 WebRTC — 设计文档

## 目标

让 PWA 浏览器通过 WebRTC 直接连接 Unitree Go2 Air（Remote/云端信令模式），无需任何本地中继进程。后端只作为信令代理（隐藏 Unitree 账号凭据），真正的 WebRTC peer 是浏览器 ↔ 机器狗。

---

## 架构

```
浏览器 (app/src/pages/Go2Page.jsx)
  1. POST /api/go2/offer  ──→  cloud/api/main.py
                                  │ 用 unitree-webrtc-connect Python 库
                                  │ 向 Unitree 云端完成账号认证
                                  │ 获取 Go2 的 SDP Offer + ICE candidates
                               ←─┘ 返回 { sdp, ice_candidates }
  2. RTCPeerConnection.setRemoteDescription(sdp)
  3. RTCPeerConnection.createAnswer() → answerSdp
  4. POST /api/go2/answer  ──→  cloud/api/main.py
                                  │ 将 answerSdp 发回 Unitree 云端
                                  │ 完成 WebRTC 握手
  5. WebRTC DataChannel 建立
  6. 浏览器直接通过 DataChannel 发指令 / 收状态
  7. 视频 track 直接渲染到 <video> 元素
```

**凭据安全**：Go2 的 Unitree 账号/密码只存在 `cloud/.env`，浏览器端不持有任何凭据。

---

## 信令代理实现原理

`unitree-webrtc-connect` Python 库内部通过 aiortc 创建 RTCPeerConnection，但我们需要的只是与 Unitree 云端的信令交互（获取 SDP Offer 和转发 Answer），不需要 Python 侧建立真正的 WebRTC 连接。

**新 session 的实现者必须先完成以下研究步骤：**

1. 阅读 `unitree-webrtc-connect` Python 库源码（PyPI 安装后在 site-packages），找到 `WebRTCConnectionMethod.Remote` 下的信令流程：
   - Unitree 云端登录接口（URL、请求体、token 获取方式）
   - 获取 Go2 SDP Offer 的接口（URL、请求体）
   - 提交浏览器 SDP Answer 的接口
   - ICE candidates 的处理方式（是否内嵌在 SDP 中，还是单独传输）
2. 基于上述信息，`/api/go2/offer` 只需调用 Unitree 云端 HTTP/WebSocket API（不用 aiortc），返回 SDP 字符串给浏览器。
3. `/api/go2/answer` 接收浏览器的 answer SDP，发回 Unitree 云端。

参考文件：`test_script/test_remote_connection.py`（已有可运行的 Remote 模式连接代码）。

---

## 新增 / 修改文件

### cloud/api/main.py — 新增信令代理端点

```python
# 新增两个端点：

@app.post("/api/go2/offer")
async def go2_offer():
    """向 Unitree 云端认证，获取 Go2 SDP Offer。"""
    # 1. 用 GO2_EMAIL/GO2_PASSWORD/GO2_SERIAL 登录 Unitree 云端
    # 2. 获取该序列号 Go2 的 SDP Offer
    # 3. 返回 {"sdp": "...", "type": "offer"}
    ...

@app.post("/api/go2/answer")
async def go2_answer(body: Go2AnswerRequest):
    """将浏览器的 SDP Answer 转发给 Unitree 云端，完成握手。"""
    # body.sdp: 浏览器生成的 answer SDP
    # 转发到 Unitree 云端信令接口
    ...
```

### cloud/.env — 新增 4 个变量

```
GO2_EMAIL=<Unitree 账号邮箱>
GO2_PASSWORD=<Unitree 账号密码>
GO2_SERIAL=B42D1000Q3JE0J8D
GO2_REGION=cn
```

### app/src/app.jsx — 2 处修改

1. 在路由分发处（line ~1417），当 `slug === 'go2-air'` 时渲染 `<Go2DevicePage>` 而不是通用 `<DeviceDetailPage>`
2. 在设备发现逻辑里，硬编码注入 Go2 设备条目（使 `#/devices/go2-air` 可从设备列表点入）：

```javascript
const GO2_STATIC_DEVICE = {
  unit_id: 'go2_main',
  agent_id: 'go2_main',
  slug: 'go2-air',
  name: 'Go2 Air',
  agent_type: 'robot',
  capabilities: ['MOVE', 'STAND_UP', 'SIT_DOWN', 'HELLO', 'STRETCH', 'DANCE'],
};
```

### app/src/pages/Go2Page.jsx — 新建

负责：
- 调用 `/api/go2/offer` → 建立 `RTCPeerConnection` → 调用 `/api/go2/answer`
- 通过 DataChannel 订阅 `LF_SPORT_MOD_STATE`，展示：连接状态、运动模式、body_height、速度
- 指令按钮：站起 / 坐下 / 挥手 / 伸展 / 舞蹈1 / 舞蹈2 / 停止
- `<video autoplay>` 渲染 WebRTC 视频 track

**DataChannel 消息格式**（参考 `test_remote_connection.py` 中的 `_publish` 函数）：
```javascript
// 发布指令
{ type: "publish", topic: RTC_TOPIC, data: { api_id: SPORT_CMD_ID, parameter: {...} } }

// 订阅状态（发送订阅请求后，Go2 持续推送）
{ type: "subscribe", topic: "lf/sportmodestate" }
```

具体的 `RTC_TOPIC` 常量和 `SPORT_CMD` api_id 值，从 Python 库的 `constants.py` 文件中提取。

---

## DataChannel 协议参考

从 `test_remote_connection.py` 可知：

| 动作 | topic | api_id |
|------|-------|--------|
| 站起 | `SPORT_MOD` | `SPORT_CMD["StandUp"]` |
| 坐下 | `SPORT_MOD` | `SPORT_CMD["StandDown"]` |
| 挥手 | `SPORT_MOD` | `SPORT_CMD["Hello"]` |
| 伸展 | `SPORT_MOD` | `SPORT_CMD["Stretch"]` |
| 舞蹈1 | `SPORT_MOD` | `SPORT_CMD["Dance1"]` |
| 舞蹈2 | `SPORT_MOD` | `SPORT_CMD["Dance2"]` |
| 停止 | `SPORT_MOD` | `SPORT_CMD["StopMove"]` |
| 移动 | `SPORT_MOD` | `SPORT_CMD["Move"]` + `{x,y,z}` |
| 切换模式 | `MOTION_SWITCHER` | 1002 + `{name}` |
| 查询模式 | `MOTION_SWITCHER` | 1001 |
| 订阅状态 | `LF_SPORT_MOD_STATE` | — |

**新 session 需要提取的常量**：运行 `python3 -c "from unitree_webrtc_connect.constants import RTC_TOPIC, SPORT_CMD; import json; print(json.dumps({**RTC_TOPIC, **SPORT_CMD}, indent=2))"` 获得所有值。

---

## 不改动的部分

- `edge/esp32/`（原 `edge/` 平铺文件）— 不动
- `cloud/orchestrator/` — 不动（Go2Agent 留到后续）
- MQTT 总线 — Go2 控制路径完全不经过 MQTT
- `protocol/topics.md` — 不更新（本期无新 MQTT topic）

---

## 验收标准

1. 打开 `http://localhost:8081/#/devices/go2-air`，看到 Go2 控制页面
2. 点击"连接"，WebRTC 握手成功（页面显示"已连接"）
3. 视频流在 `<video>` 中播放
4. 点击"站起"按钮，机器狗站起来
5. 状态栏实时显示 body_height 和速度

---

## 风险与注意事项

- **信令协议逆向**：核心风险在于 Unitree 云端信令 API 的细节，需从 Python 库源码提取。若 API 较复杂（如有 WebSocket 长连接要求），可能需要在 FastAPI 里做 WebSocket 代理而非 HTTP 代理。
- **ICE candidates**：Unitree 可能将 ICE candidates 内嵌在 SDP 中（all-in-one SDP），也可能需要单独交换。需从源码确认。
- **同时只能有一个 WebRTC 客户端**：Go2 被占用时会抛 `RobotBusyError`，API 需处理并返回 409。
- **视频格式**：WebRTC 视频 track 的编解码由浏览器自动处理，只需设置正确的 MIME type。
