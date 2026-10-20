# SSM MQTT 协议契约

所有智能体通过以下 topic 通信。改动 topic 格式必须同步更新本文件。

## Topic 命名规则

标识符定义见 [`identifiers.md`](identifiers.md)。要点：

```
ssm/agents/{unit_id}/{type}   # 单元级
ssm/agents/{device_id}/{type} # 整机级（status / location）
```

- `unit_id`：传输单元唯一标识，`{device_id}_{unit}`，如 `esp32_desk_led`。**所有 topic 段、payload 自标识键一律用它。**
- `device_id`：整机 / 连接，如 `esp32_desk`；仅用于 status / location / rules。
- `type`：`state`（retained）、`event`、`manifest`、`status`。
- payload 自标识键一律为 `unit_id`。

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
| `ssm/agents/{unit_id}/state` | ESP32 | `{"unit_id": "...", "ism": "OFF\|BRIGHT\|DIM\|COLOR", "ts": 1234567}` |

### 传感器事件

| Topic | 发布者 | 格式 |
|-------|--------|------|
| `ssm/agents/{unit_id}/event` | ESP32 | 光线：`{"unit_id":"...","value":1800,"level":"NORMAL","ts":...}` |
| `ssm/agents/{unit_id}/event` | ESP32 | 声音：`{"unit_id":"...","detected":true,"ts":...}` |

### 指令下发

| Topic | 发布者 | 格式 |
|-------|--------|------|
| `ssm/task/{unit_id}/{task_id}` | Cloud | `{"action":"SET_STATE","params":{"state":"..."},"session_id":"...","actor":"admin\|visitor","persist":false,"space_id":"main"}`；不携带云端 service token，发布者由 broker ACL 限制为 API/编排器账号 |
| `ssm/result/{unit_id}/{task_id}` | ESP32 | `{"task_id":"...","session_id":"...","result":"ok\|blocked","ism_state":"...","ts":...}`；旧固件可省略 `session_id`，编排器按已登记任务归属补齐 |

> 注：ESP32 云端离线时由 `local_rules` 直接调用 `TriggerMap.exec_command` 执行兜底动作（设备内函数调用，不经 MQTT）。

### 编排器

| Topic | 发布者 | 格式 |
|-------|--------|------|
| `ssm/intent/{session_id}` | FastAPI（内部） | `{"text":"...","context":{...},"_mqtt_auth":{"alg":"HMAC-SHA256","timestamp":...,"nonce":"...","signature":"..."}}`；签名绑定完整 topic 与规范化 payload，编排器校验时间窗并拒绝 nonce 重放 |
| `ssm/session/{session_id}/ended` | FastAPI（内部） | `{"context":{...},"reason":"logout\|expired\|revoked","_mqtt_auth":{...}}`；与 intent 使用相同 HMAC 验证，触发会话历史、任务结果与 Go2 资源清理 |
| `ssm/feedback/{session_id}` | Cloud | `{"session_id":"...","stage":"planning\|ack\|executing\|done\|partial\|failed\|denied","text":"...","status":"ok\|denied","ts":...}`；API/SSE 只向同一会话转发 |
| `ssm/decision/active` | Cloud | `"true"` 或 `"false"` |
| `ssm/rules/{device_id}` | Cloud（retained）| `[{"id":"...","en":true,"trig":{...},"act":{...}}]` |

### 拟人化表达（桌面灯智能体 / Go2）

| Topic | 发布者 | 格式 |
|-------|--------|------|
| `ssm/agents/esp32_desk_led/speech` | Cloud | `{"text":"...","priority":"normal","audio":"base64..."}` |
| `ssm/agents/{unit_id}/thought` | Cloud | `{"text":"...","type":"think\|act"}`；灯用 `esp32_desk_led`，机器狗用 `go2` |
