# SSM MQTT 协议契约

智能体与传感输入通过以下 topic 通信。改动 topic 格式必须同步更新本文件。

## Topic 命名规则

标识符定义见 [`identifiers.md`](identifiers.md)。要点：

```
ssm/agents/{unit_id}/{type}   # 单元级
ssm/agents/{device_id}/{type} # 整机级（status / location）
```

- `unit_id`：传输单元唯一标识，`{device_id}_{unit}`，如 `esp32_desk_led`。**所有 topic 段、payload 自标识键一律用它。**
- `device_id`：整机 / 连接，如 `esp32_desk`；仅用于 status / location / rules。
- `type`：项目定义的消息类型；传感输入的业务消息只有 `state`（retained）和 `event`，智能体另可发布 `manifest` 或 `card`，整机发布 `status`。
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
| `ssm/agents/{unit_id}/manifest` | ESP32 智能体（当前为灯） | `{"unit_id": "...", "parent_id":"...", "agent_type": "actuator", "tags":[...], "capabilities": [...], "ts": ...}`；灯的新版 manifest 带 `"fsm_version":2`，云端据此选择卡片；空 payload = 单元缺席或撤销旧声明。传感器不再发布非空 manifest；新固件启动/重连时发送一次空 retained payload 清理旧声明，云端发现层也忽略旧传感器 manifest。 |
| `ssm/agents/{unit_id}/card` | Go2 / 云端编排器（retained）| 完整 AgentCard JSON；空 payload = 撤销能力声明。编排器使用内部 `unit_id=cloud_orchestrator`、`agent_type=decision`，仅进入同伴视图，不进入物理设备列表 |
| `ssm/agents/cloud_orchestrator/status` | 云端编排器（retained + LWT） | `"online"` 或 `"offline"`；同伴请求可用性以此为准，不能以保留的 card 推断在线 |

### 设备状态（retained）

| Topic | 发布者 | 格式 |
|-------|--------|------|
| `ssm/agents/{unit_id}/state` | ESP32 | 灯：`{"unit_id":"esp32_desk_led","ism":"ON","r":255,"g":200,"b":80,"brightness":180,"ts":1234567890}`；`OFF` 时 RGB/亮度全为 0；`BLINKING` 时附带 `count`，RGB/亮度是闪烁配置而非瞬时相位。旧固件的 `BRIGHT/DIM/COLOR/BLINK` 在迁移期仍可被展示。传感器继续在各自 unit_id 下发送独立的当前读数，不合并到灯的状态。 |

### 传感器事件

传感器是灯智能体的感知输入，不参与 Agent Card 发现；云端灯大脑继续订阅它们的 `state/event`。PWA 通过 API/SSE 获取设备信息，不直接连接 MQTT。

| Topic | 发布者 | 格式 |
|-------|--------|------|
| `ssm/agents/{unit_id}/event` | ESP32 | 光线：`{"unit_id":"...","value":1800,"level":"NORMAL","ts":...}` |
| `ssm/agents/{unit_id}/event` | ESP32 | 声音：`{"unit_id":"...","detected":true,"ts":...}` |

### 指令下发

| Topic | 发布者 | 格式 |
|-------|--------|------|
| `ssm/task/{unit_id}/{task_id}` | Cloud | `{"task_id":"...","session_id":"...","action":"SET_STATE","params":{"state":"ON"},"actor":"admin\|visitor\|agent","persist":false,"space_id":"main","ts":1234567890}`；灯的正式命令为 `SET_STATE`（`ON/OFF`）、`SET_COLOR`（`r/g/b/brightness`）、`BLINK`（`r/g/b/count`）。旧固件的 `SET_STATE BRIGHT/DIM` 仅在迁移期兼容；不携带云端 service token，发布者由 broker ACL 限制为 API/编排器账号 |
| `ssm/result/{unit_id}/{task_id}` | ESP32 | `{"task_id":"...","session_id":"...","result":"executing\|ok\|cancelled\|blocked\|error","ism_state":"ON","ts":...}`；旧固件可省略 `session_id`，编排器按已登记任务归属补齐 |

> 注：ESP32 云端离线时由 `local_rules` 直接调用 `TriggerMap.exec_command` 执行兜底动作（设备内函数调用，不经 MQTT）。

### 灯的语义技能（控制面 Card，数据面 HTTP → MQTT）

灯 manifest 含 `SET_COLOR` 时，云端 Card builder 额外公开 `adjust_lighting(goal)`。Card 的设备级 `transport` 仍是 MQTT；该技能的 `invoke.transport` 可覆盖为受校验的同源路径 `/api/internal/esp32/{unit_id}/agent/tasks`。其余灯技能仍按 MQTT `ssm/task/{unit_id}/{task_id}` 派发。Card 校验只允许灯自身的内部语义端点接收服务令牌，禁止外部 URL 和跨 unit 路径。

编排器把定性照明目标作为 `goal` 发给灯大脑；灯规划具体 RGB、亮度和动作，并通过现有 MQTT 数据面执行。语义请求持有原 `task_id`，服务端将它关联到物理 `task_id`；`accepted` 或 `executing` 均不表示完成，只有 ESP32 `ssm/result/{unit_id}/{task_id}` 的 `ok` 才能对用户确认。HTTP 超时后查询 `/api/internal/esp32/{unit_id}/agent/tasks/{task_id}`，不重新提交物理命令。

设备页按钮收到的物理 `task_id` 可由同一浏览器会话查询 `GET /api/commands/{unit_id}/tasks/{task_id}`；无回执时返回 `executing`，实际 `ssm/result` 到达后返回最终状态。会话结束后任务查询权限随之撤销，查询绝不重发 MQTT 任务。

### 编排器

| Topic | 发布者 | 格式 |
|-------|--------|------|
| `ssm/intent/{session_id}` | FastAPI（内部） | `{"text":"...","context":{...},"_mqtt_auth":{"alg":"HMAC-SHA256","timestamp":...,"nonce":"...","signature":"..."}}`；签名绑定完整 topic 与规范化 payload，编排器校验时间窗并拒绝 nonce 重放 |
| `ssm/session/{session_id}/ended` | FastAPI（内部） | `{"context":{...},"reason":"logout\|expired\|revoked","_mqtt_auth":{...}}`；与 intent 使用相同 HMAC 验证，触发会话历史、任务结果与 Go2 资源清理 |
| `ssm/feedback/{session_id}` | Cloud | `{"session_id":"...","stage":"planning\|ack\|executing\|done\|partial\|failed\|denied","text":"...","status":"ok\|denied","ts":...}`；API/SSE 只向同一会话转发 |
| `ssm/decision/active` | Cloud | `"true"` 或 `"false"` |
| `ssm/rules/{device_id}` | Cloud（retained）| `[{"id":"...","en":true,"trig":{...},"act":{...}}]` |

### 智能体同伴协调请求（内部，非 retained）

同伴对灯和 Go2 的单技能请求由 API 可信网关校验后走各自现有 MQTT task/result 或 HTTP 数据面。仅请求编排器的 `coordinate_request` 使用下列独立主题；不得伪装为 `ssm/intent/{session_id}`。请求与结果均使用与 intent 相同的 `_mqtt_auth` HMAC 封套签名（签名绑定 topic、规范化 payload、时间戳和 nonce），接收方校验签名、重放和有效期。Broker ACL 应只允许 API 账号发布 request、编排器账号发布 result；禁止终端直接发布。service token 不进入 MQTT payload。

编排器收到已验证的用户 intent 时取消尚未派发的自主协调步骤。已送达设备的动作仍按目标回执和自身停止能力处理。

| Topic | 发布者 | 格式 |
|-------|--------|------|
| `ssm/peer/request/{request_id}` | FastAPI（内部，QoS 1） | `{"request_id":"...","initiator_unit_id":"go2|esp32_desk_led","target_unit_id":"cloud_orchestrator","skill_id":"coordinate_request","params":{"message":"..."},"origin_kind":"user_delegated|agent_autonomous","origin_session_id":"...|null","actor":"admin|visitor|agent","persist":false,"space_id":"main","issued_at":... ,"expires_at":... ,"_mqtt_auth":{...}}`。发起者、角色、空间和委托来源均由 API 可信上下文填入，模型传值不可信；单跳委托，编排器执行过程中不能在同一链转发同伴请求 |
| `ssm/peer/result/{request_id}` | 云端编排器（内部，QoS 1） | `{"request_id":"...","initiator_unit_id":"...","target_unit_id":"cloud_orchestrator","result":"ok|blocked|error|timeout|cancelled|executing","response":"...","task_results":{},"ts":...,"_mqtt_auth":{...}}`。`executing` 不是完成；Go2 的设备 `accepted` 也不可折算成 `ok`。同一 `request_id` 重复投递不得重复执行物理动作，超时查询已登记结果，不盲目重发 |
| `ssm/peer/cancel/{request_id}` | FastAPI（内部，QoS 1） | `{"request_id":"...","reason":"user_takeover|session_ended|mode_changed|target_offline","ts":...,"_mqtt_auth":{...}}`。仅取消尚未派发的后续协调步骤；已送达的物理动作由目标设备处理 |

### 拟人化表达（桌面灯智能体 / Go2）

| Topic | 发布者 | 格式 |
|-------|--------|------|
| `ssm/agents/esp32_desk_led/speech` | Cloud | `{"text":"...","priority":"normal","audio":"base64..."}` |
| `ssm/agents/{unit_id}/thought` | Cloud | `{"text":"...","type":"think\|act"}`；灯消息另带 `unit_id` 和 `ts`，灯用 `esp32_desk_led`，机器狗用 `go2`；API 将空间内灯的 thought 转发到 PWA 的 SSE 活动流 |
