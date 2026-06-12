# SSM MQTT 协议契约

所有智能体通过以下 topic 通信。改动 topic 格式必须同步更新本文件。

## Topic 命名规则

标识符定义见 [`identifiers.md`](identifiers.md)。要点：

```
ssm/agents/{unit_id}/{type}   # 单元级
ssm/agents/{device_id}/{type} # 整机级（status / location）
```

- `unit_id`：传输单元唯一标识，`{device_id}_{unit}`，如 `esp32_desk_led`。**所有 topic 段、payload 自标识键一律用它。**
- `device_id`：整机 / 连接，如 `esp32_desk`；仅用于 status / location / rules / pong。
- `type`：`state`（retained）、`event`、`report`、`command`、`manifest`、`status`。
- ⚠️ payload 自标识键应为 `unit_id`；ESP32 固件当前仍用旧键 `agent`（待收敛）。

## 核心 Topic 列表

### 在线状态（retained，LWT）

| Topic | 发布者 | 格式 |
|-------|--------|------|
| `ssm/agents/{device_id}/status` | ESP32 / Go2 | `"online"` 或 `"offline"`（设备掉线时 LWT 自动置 offline）|
| `ssm/agents/{device_id}/location` | ESP32 | `{"unit_id":"...","lng":...,"lat":...,"type":"fixed","ts":...}` |

### 设备能力（retained）

| Topic | 发布者 | 格式 |
|-------|--------|------|
| `ssm/agents/{unit_id}/manifest` | ESP32 | `{"unit_id": "...", "parent_id":"...", "agent_type": "...", "tags":[...], "capabilities": [...], "ts": ...}`；空 payload = 单元缺席移除 |
| `ssm/agents/{unit_id}/card` | Go2（retained）| 完整 AgentCard JSON；空 payload = 离线移除 |

### 设备状态（retained）

| Topic | 发布者 | 格式 |
|-------|--------|------|
| `ssm/agents/{unit_id}/state` | ESP32 | `{"agent": "...", "ism": "OFF\|BRIGHT\|DIM\|COLOR", "ts": 1234567}` |

### 传感器事件

| Topic | 发布者 | 格式 |
|-------|--------|------|
| `ssm/agents/{unit_id}/event` | ESP32 | 光线：`{"agent":"...","value":1800,"level":"NORMAL","ts":...}` |
| `ssm/agents/{unit_id}/event` | ESP32 | 声音：`{"agent":"...","detected":true,"ts":...}` |
| `ssm/agents/{unit_id}/report` | ESP32 | 同 event，加 `"type":"observation"` |

### 指令下发

| Topic | 发布者 | 格式 |
|-------|--------|------|
| `ssm/agents/{unit_id}/command` | Cloud | `{"cmd":"SET_STATE","state":"BRIGHT\|DIM\|OFF"}` |
| `ssm/task/{unit_id}/{task_id}` | Cloud | `{"action":"SET_STATE","params":{"state":"..."},"session_id":"..."}` |
| `ssm/result/{unit_id}/{task_id}` | ESP32 | `{"task_id":"...","result":"ok\|blocked","ism_state":"...","ts":...}` |

### 编排器

| Topic | 发布者 | 格式 |
|-------|--------|------|
| `ssm/feedback/{session_id}` | Cloud | `{"stage":"planner\|dispatcher\|evaluator\|responder","text":"...","status":"ok"}` |
| `ssm/decision/active` | Cloud | `"true"` 或 `"false"` |
| `ssm/rules/{device_id}` | Cloud（retained）| `[{"id":"...","en":true,"trig":{...},"act":{...}}]` |

### DeskAgent 专属

| Topic | 发布者 | 格式 |
|-------|--------|------|
| `ssm/agents/desk/speech` | Cloud | `{"text":"...","priority":"normal","audio":"base64..."}` |
| `ssm/agents/desk/led_mood` | Cloud | `{"mood":"thinking\|speaking\|done\|idle"}` |
| `ssm/agents/desk/thought` | Cloud | `{"text":"..."}` |

### 系统

| Topic | 用途 |
|-------|------|
| `ssm/sys/ping` | 心跳探测 |
| `ssm/sys/pong/{device_id}` | 心跳响应 |
