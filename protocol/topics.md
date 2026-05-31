# SSM MQTT 协议契约

所有智能体通过以下 topic 通信。改动 topic 格式必须同步更新本文件。

## Topic 命名规则

```
ssm/agents/{unit_id}/{type}
```

- `unit_id`：`{device_id}_{unit}`，如 `esp32_desk_led`
- `type`：`state`（retained）、`event`、`report`、`command`

## 核心 Topic 列表

### 设备状态（retained）

| Topic | 发布者 | 格式 |
|-------|--------|------|
| `ssm/agents/{unit_id}/state` | ESP32 | `{"agent": "...", "ism": "OFF\|BRIGHT\|DIM\|COLOR", "ts": 1234567}` |
| `ssm/agents/{unit_id}/manifest` | ESP32 | `{"unit_id": "...", "agent_type": "...", "capabilities": [...], "ts": ...}` |

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
| `ssm/rules/{agent_id}` | Cloud（retained）| `[{"id":"...","en":true,"trig":{...},"act":{...}}]` |

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
| `ssm/sys/pong/{agent_id}` | 心跳响应 |
