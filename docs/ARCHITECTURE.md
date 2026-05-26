# SSM 系统架构文档

> 最后更新：2026-05-09
> 随代码变更同步维护，修改任何 topic / 设备 / 网络配置时请同步更新本文件。

---

## 一、整体技术架构

```
┌──────────────────────────────────────────────────────────────────┐
│  手机浏览器 (HTTPS/WSS)                                          │
│  PWA — agents/phone/                                             │
└─────────────────────────┬────────────────────────────────────────┘
                          │ HTTPS / WSS
                          ▼
              ┌───────────────────────┐
              │  ngrok 公网隧道        │  https://xxx.ngrok-free.dev
              │  (重启后地址变化)      │
              └───────────┬───────────┘
                          │ HTTP (port 8080)
                          ▼
              ┌───────────────────────┐
              │  nginx 反向代理        │  /etc/nginx/conf.d/ssm.conf
              │  listen :8080         │
              ├───────────────────────┤
              │ /        → :8081      │  PWA 静态文件 (uv python -m http.server)
              │ /api/    → :8082      │  FastAPI server/api/main.py
              │ /mqtt    → :9001      │  MQTT over WebSocket
              └───────────┬───────────┘
                          │
              ┌───────────┴───────────┐
              │  Mosquitto Broker     │  broker/mosquitto.conf
              │  TCP  :1883           │  ← ESP32 直连
              │  WS   :9001           │  ← PWA (经 nginx)
              └───────────┬───────────┘
                  ┌───────┴───────┐
                  │               │
        ┌─────────┴──┐     ┌──────┴──────────┐
        │  ESP32      │     │  PC Agent        │
        │ (MicroPy)   │     │  server/orchestrator/
        │ :1883 TCP   │     │  :1883 TCP       │
        └────────────┘     └──────────────────┘
```

### 服务进程一览

| 进程 | 启动命令 | 端口 | 管理方式 |
|------|---------|------|---------|
| Mosquitto Broker | `mosquitto -c /root/ssm/broker/mosquitto.conf -d` | 1883, 9001 | 手动 / 持久运行 |
| PWA 静态文件 | `make pwa-bg` | 8081 | nohup |
| FastAPI Chat API | `make api-bg` | 8082 | nohup |
| nginx | `systemctl start nginx` | 8080 | systemd |
| ngrok 隧道 | `make ngrok-bg` | — | nohup |
| PC Agent | `make orchestrator-bg` | — | nohup |

---

## 二、设备列表

| 设备 ID | 类型 | 平台 | 代码路径 |
|---------|------|------|---------|
| `esp32_desk` | 父设备（ESP32 主机） | MicroPython | `agents/esp32/` |
| `esp32_desk_light` | 传感器 — 光线 (ADC) | ESP32 子 unit | `agents/esp32/` |
| `esp32_desk_ir` | 传感器 — 红外 | ESP32 子 unit | `agents/esp32/` |
| `esp32_desk_sound` | 传感器 — 声音 | ESP32 子 unit | `agents/esp32/` |
| `esp32_desk_led` | 执行器 — WS2812 灯环 | ESP32 子 unit | `agents/esp32/` |
| `esp32_desk_buz` | 执行器 — 蜂鸣器 | ESP32 子 unit | `agents/esp32/` |
| `pc_llm_agent` | PC 决策智能体 | Python / PC | `server/orchestrator/` |
| `phone_ui` | 手机监控 + 对话 | PWA / Browser | `agents/phone/` |

---

## 三、四条事件路径总览

PC Agent（`server/orchestrator/main.py`）维护一个事件队列，所有 MQTT 消息经 `on_message` 分发为四类事件：

```
MQTT 消息入队
  │
  ├─ ssm/agents/+/event  (suffix: light/ir/sound)
  │       └─ trigger="sensor"   → 规则引擎（零 LLM）
  │
  ├─ ssm/agents/+/report (suffix: led/buz 等执行器)
  │       └─ trigger="actuator" → 评估图（V1，静默）
  │
  ├─ ssm/intent/{session_id}
  │       └─ trigger="intent"   → V2 编排器（用户意图）
  │
  └─ ssm/result/{device_id}/{task_id}
          └─ 直接写入 SharedState，供 Evaluator 轮询
```

### 路径对比

| 维度 | 路径 A：规则引擎 | 路径 B：V2 编排器 | 路径 C：V1 评估 |
|------|----------------|-----------------|----------------|
| **触发** | `ssm/agents/+/event`（传感器）| `ssm/intent/{session_id}` | `ssm/agents/+/report`（执行器）|
| **入口** | `rule_engine.match_and_fire()` | `orchestrator.invoke()` | `evaluation_graph.invoke()` |
| **LLM** | ❌ | ✅ Planner + Responder | ✅ Evaluation ReAct |
| **延迟** | <5ms | 5~15s | 5~10s |
| **用户可见** | ❌ 静默 | ✅ 渐进反馈 | ❌ 静默 |
| **配置方式** | PWA 规则页 / NLU 对话 | 直接对话 | 无需配置 |
| **条件** | PC Agent 在线且有匹配规则 | 用户主动发消息 | 执行器完成任务后 |

---

## 四、用户问问题：完整路由系统

用户在 PWA 输入文字时，经历以下完整路由链路：

### 4.1 全链路时序图

```
手机用户输入文字
  │
  │ POST /api/nlu  {message, devices}
  ▼
┌─────────────────────────────────────────────────────────────────┐
│  FastAPI /api/nlu  (server/api/main.py)                        │
│                                                                 │
│  LLM 意图分类（NLU_SYSTEM prompt）                              │
│  ┌──────────────────────────────────────────────────────────┐  │
│  │ 判断 intent_type                                         │  │
│  │                                                          │  │
│  │  "execute"     → requirements[]（资源标签 + 操作意图）   │  │
│  │  "define_rule" → rule{}（触发条件 + 执行动作）           │  │
│  └──────────────────────────────────────────────────────────┘  │
│                                                                 │
│  返回: {session_id, intent_type, nlu_feedback, requirements/rule}│
└─────────────────────────────────────────────────────────────────┘
  │
  │ 返回给 PWA
  ▼
┌─────────────────────────────────────────────────────────────────┐
│  PWA 前端路由（app.jsx ChatSheet）                              │
│                                                                 │
│  intent_type == "define_rule"                                   │
│      → 展示规则预览卡，等待用户确认                             │
│      → 用户点确认 → POST /api/rules → 规则持久化 + 推送 ESP32  │
│                                                                 │
│  intent_type == "execute"                                       │
│      → 订阅 ssm/feedback/{session_id}（30s 超时）              │
│      → 发布 ssm/intent/{session_id}                            │
│          {user_msg, requirements, session_id, ts}               │
└─────────────────────────────────────────────────────────────────┘
  │
  │ MQTT: ssm/intent/{session_id}
  ▼
┌─────────────────────────────────────────────────────────────────┐
│  PC Agent on_message → event_queue.put(trigger="intent")       │
└─────────────────────────────────────────────────────────────────┘
  │
  ▼
┌─────────────────────────────────────────────────────────────────┐
│  V2 Orchestrator（graph.py build_orchestrator）                 │
│                                                                 │
│  ① planner_node                                                │
│     ├─ 读 SharedState.get_capability_registry()（设备能力表）  │
│     ├─ LLM 推理：requirements → planned_tasks[]                │
│     │   requirement.resource_tag 匹配设备 agent_tag            │
│     │   requirement.action 映射为 ESP32 指令（见映射表）       │
│     └─ 发布 feedback stage=planning                            │
│                                                                 │
│  ② dispatcher_node                                             │
│     ├─ 发布 ssm/task/{device_id}/{task_id}（每个任务一条）     │
│     └─ 发布 feedback stage=executing                           │
│                                                                 │
│  ③ evaluator_node                                              │
│     ├─ 轮询 SharedState.get_task_result(task_id)（最多 5s）   │
│     └─ 超时未响应的任务填 result="timeout"                     │
│                                                                 │
│  ④ responder_node                                              │
│     ├─ 统计 ok/partial/failed                                  │
│     ├─ LLM 生成自然语言回复（1 句话）                          │
│     └─ 发布 feedback stage=done|partial|failed                 │
└─────────────────────────────────────────────────────────────────┘
  │
  │ MQTT: ssm/feedback/{session_id}
  ▼
┌──────────────────────────────────────┐
│  PWA ChatSheet 收到反馈，实时更新气泡 │
└──────────────────────────────────────┘
```

### 4.2 NLU 意图分类规则

| 输入特征 | intent_type | 说明 |
|---------|-------------|------|
| 一次性操作（"开灯"、"调暗"、"播报警"）| `execute` | 直接进入 V2 编排路径 |
| 含"以后"、"每次"、"当…就"、"自动"等词 | `define_rule` | 进入规则定义预览流程 |

### 4.3 action → ESP32 指令映射（Planner 规则）

| requirements.action | ESP32 指令 | params |
|--------------------|-----------|--------|
| `brighten` / `on` | `SET_STATE` | `state=BRIGHT` |
| `dim` | `SET_STATE` | `state=DIM`（禁止先 OFF 再 DIM）|
| `off` | `SET_STATE` | `state=OFF` |
| `set_color` | `SET_COLOR` | `r/g/b/brightness`（场景自动推断）|
| `notify` | `PLAY` | `pattern=NOTIFY` |
| `alert` | `PLAY` | `pattern=ALERT` |
| `blink` | `BLINK` | `r/g/b/count` |

### 4.4 Planner early_exit 条件

| 条件 | 后续行为 |
|------|---------|
| `get_capability_registry()` 为空（无设备在线）| 直接发布 `stage=failed`，跳过后续节点 |
| `planned_tasks` 为空（无匹配设备）| Dispatcher 发布 `stage=failed`，跳过 Evaluator/Responder |
| 所有任务 result=ok | Responder 发布 `stage=done` |
| 部分 result=ok | Responder 发布 `stage=partial` |
| 全部 result=timeout/blocked | Responder 发布 `stage=failed` |

---

## 五、规则引擎详解

### 5.1 规则数据流

```
用户说"检测到人就开灯"
  │
  ▼
POST /api/nlu → intent_type: "define_rule"
  │                rule: {name, trigger, action}
  ▼
PWA 展示规则预览卡 → 用户确认
  │
  ▼
POST /api/rules → 写入 server/orchestrator/rules.json
  │
  ▼
_push_rules_to_esp32()
  └─ 发布 ssm/rules/esp32_desk [retain] 精简规则列表
       │
       ▼
     ESP32 local_rules.load_rules()
       └─ 写入 rules_cache.json（flash 持久化）
```

### 5.2 规则引擎执行流（传感器 event 到达时）

```
ssm/agents/{sensor_id}/event
  │
  ▼
PC Agent on_message → event_queue.put(trigger="sensor")
  │
  ▼
rule_engine.match_and_fire(unit_id, payload)
  │
  ├─ 读 manifest.agent_tag（从 SharedState）
  ├─ 热加载 rules.json
  ├─ 遍历规则：trigger.agent_tag == agent_tag ?
  │     └─ _EVENT_MATCHERS[(agent_tag, event)] 匹配 payload ?
  │           └─ 匹配 → 查 capability_registry[resource_tag]
  │                      → do_publish_task → ssm/task/{device_id}/{task_id}
  └─ 返回 fired=True/False（True 则不进入 LLM 路径）
```

### 5.3 规则格式

**云端存储格式**（`rules.json`）：
```json
{
  "rule_id": "r_1714200000_a1b2c3",
  "name": "检测到人就开灯",
  "enabled": true,
  "trigger": {"agent_tag": "presence", "event": "detected"},
  "action": {"resource_tag": "lighting", "cmd": "SET_STATE", "params": {"state": "BRIGHT"}},
  "created_at": 1714200000
}
```

**ESP32 精简格式**（`rules_cache.json` / `ssm/rules/{agent_id}`）：
```json
{"id": "r_1714200000_a1b2c3", "en": true,
 "trig": {"tag": "presence", "ev": "detected"},
 "act": {"cmd": "SET_STATE", "state": "BRIGHT"}}
```

### 5.4 规则 CRUD API

| 方法 | 路径 | 说明 |
|------|------|------|
| `GET` | `/api/rules` | 获取所有规则 |
| `POST` | `/api/rules` | 创建规则（并推送到 ESP32）|
| `DELETE` | `/api/rules/{rule_id}` | 删除规则（并推送到 ESP32）|
| `PATCH` | `/api/rules/{rule_id}/toggle?enabled=bool` | 启用/禁用（并推送到 ESP32）|

### 5.5 支持的触发条件

| agent_tag | event | 触发条件 |
|-----------|-------|---------|
| `presence` | `detected` | 红外检测到人 |
| `presence` | `disappeared` | 人离开 |
| `light_level` | `dark` | 光线变暗（DARK / DIM）|
| `light_level` | `bright` | 光线变亮（BRIGHT / NORMAL）|
| `light_level` | `changed` | 任意光线变化 |
| `sound` | `detected` | 声音传感器触发 |

### 5.6 离线可靠性

- ESP32 启动时从 flash 读取 `rules_cache.json`，**立即生效，无需等待 MQTT**
- 重连时再次从 flash 加载，随后 broker 的 retained 消息会覆盖更新版本
- 最多缓存 10 条规则（内存约束）
- PC Agent 在线时发布 `ssm/decision/active = "true"`，ESP32 本地规则自动静默；PC 断线时 LWT 自动恢复为 `"false"`，本地规则重新激活

---

## 六、V1 评估图（执行效果评估）

```
ssm/agents/{actuator_id}/report
  │
  ▼
PC Agent on_message → event_queue.put(trigger="actuator")
  │
  ▼
evaluation_graph.invoke({payload})
  │
  ▼
evaluation_react（ReAct agent）
  ├─ get_last_decision()       ← 查上次下发的指令
  ├─ get_actuator_snapshot()   ← 查执行器实际反馈
  └─ publish_assessment()      ← 发布评估结论（静默，不通知用户）
       └─ ssm/decision/evaluation
```

评估结论枚举：`ok`（正常执行）| `blocked`（ISM 拒绝转换）| `mismatch`（意图与结果不一致）

---

## 七、MQTT Topic 全表

### 7.1 标准 Agent 消息（所有设备遵循）

| Topic 模式 | 方向 | Retain | 说明 |
|-----------|------|--------|------|
| `ssm/agents/{unit_id}/manifest` | 设备 → Broker | ✅ | 设备能力声明，启动时发布 |
| `ssm/agents/{unit_id}/state` | 设备 → Broker | ✅ | 当前 ISM 状态，变更时发布 |
| `ssm/agents/{unit_id}/event` | 设备 → Broker | ❌ | 状态跳变通知（传感器）|
| `ssm/agents/{unit_id}/report` | 设备 → Broker | ❌ | 传感器读数 / 执行器反馈 |
| `ssm/agents/{unit_id}/status` | 设备 → Broker | ✅ | `online` / `offline`（Last Will）|
| `ssm/agents/{unit_id}/location` | 设备 → Broker | ✅ | GCJ-02 坐标，启动时发布 |

### 7.2 规则同步

| Topic | 方向 | Retain | 说明 |
|-------|------|--------|------|
| `ssm/rules/{agent_id}` | PC Agent → ESP32 | ✅ | 精简规则列表，规则 CRUD 后推送 |

### 7.3 控制指令

| Topic | 方向 | Retain | 说明 |
|-------|------|--------|------|
| `ssm/agents/{unit_id}/command` | PC Agent → ESP32 | ❌ | V1 直接指令（SET_COLOR / BLINK 等）|
| `ssm/decision/active` | PC Agent → 全体 | ✅ | `"true"` = PC 接管，ESP32 本地规则静默 |
| `ssm/decision/evaluation` | PC Agent → Broker | ❌ | 执行效果评估结论 |

### 7.4 意图 + 任务（V2 路径）

| Topic | 方向 | Retain | 说明 |
|-------|------|--------|------|
| `ssm/intent/{session_id}` | Phone → Broker | ❌ | 用户自然语言意图（NLU 解析后）|
| `ssm/feedback/{session_id}` | PC Agent → Phone | ❌ | 渐进式反馈（planning/executing/done 等）|
| `ssm/task/{device_id}/{task_id}` | PC Agent → ESP32 | ❌ | 编排任务（action + params）|
| `ssm/result/{device_id}/{task_id}` | ESP32 → PC Agent | ❌ | 任务执行结果（ok/blocked/timeout）|

### 7.5 系统维护

| Topic | 方向 | 说明 |
|-------|------|------|
| `ssm/sys/ping` | 任意 → ESP32 | Ping 检测 |
| `ssm/sys/pong/{unit_id}` | ESP32 → 任意 | Pong 响应 |

---

## 八、各设备发布 / 订阅详表

### ESP32（esp32_desk）

**订阅：**
```
ssm/agents/esp32_desk_led/command      ← V1 LED 控制指令
ssm/agents/esp32_desk_buz/command      ← V1 蜂鸣器控制指令
ssm/task/esp32_desk_led/+              ← V2 LED 任务
ssm/task/esp32_desk_buz/+             ← V2 蜂鸣器任务
ssm/decision/active                    ← PC Agent 接管标志
ssm/rules/esp32_desk                   ← 云端规则同步（retain，重连自动恢复）
ssm/sys/ping                           ← 系统 Ping
```

**发布：**
```
ssm/agents/esp32_desk/location         [retain] 固定坐标 GCJ-02

ssm/agents/esp32_desk_light/manifest   [retain]
ssm/agents/esp32_desk_light/state      [retain] {level: DARK|DIM|NORMAL|BRIGHT}
ssm/agents/esp32_desk_light/event             光线等级变化时
ssm/agents/esp32_desk_light/report            每次采样观测值

ssm/agents/esp32_desk_ir/manifest      [retain]
ssm/agents/esp32_desk_ir/state         [retain] {presence: true|false}
ssm/agents/esp32_desk_ir/event                存在状态变化时
ssm/agents/esp32_desk_ir/report               每次采样观测值

ssm/agents/esp32_desk_sound/manifest   [retain]
ssm/agents/esp32_desk_sound/event             检测到声音时
ssm/agents/esp32_desk_sound/report            检测到声音时

ssm/agents/esp32_desk_led/manifest     [retain]
ssm/agents/esp32_desk_led/state        [retain] {ism: OFF|DIM|BRIGHT|COLOR|BLINK}
ssm/agents/esp32_desk_led/report              command 执行反馈 {cmd, result, ism_state}

ssm/agents/esp32_desk_buz/manifest     [retain]
ssm/agents/esp32_desk_buz/state        [retain] {ism: SILENT|ALERT|NOTIFY}
ssm/agents/esp32_desk_buz/report              command 执行反馈 {cmd, result, ism_state}

ssm/result/esp32_desk_led/{task_id}           V2 任务执行结果
ssm/result/esp32_desk_buz/{task_id}           V2 任务执行结果

ssm/sys/pong/esp32_desk_led                   Ping 响应
```

---

### PC Agent（pc_llm_agent）

**订阅：**
```
ssm/agents/+/manifest      ← 发现所有设备，构建能力注册表
ssm/agents/+/state         ← 跟踪所有 ISM 状态
ssm/agents/+/event         ← 传感器事件 → 规则引擎匹配
ssm/agents/+/report        ← 执行器反馈 → V1 评估图
ssm/decision/active        ← 监听自身控制标志
ssm/intent/+               ← 手机意图 → V2 Orchestrator
ssm/result/+/+             ← V2 任务结果 → SharedState
```

**发布：**
```
ssm/decision/active                    [retain] "true" 上线 / LWT "false" 下线
ssm/agents/pc_llm_agent/manifest              自身能力声明
ssm/agents/pc_llm_agent/state                 ACTIVE 状态

ssm/rules/{agent_id}                   [retain] 规则变更后推送精简规则列表
ssm/agents/{unit_id}/command                  V1 直接控制指令
ssm/decision/evaluation                       V1 执行评估结论
ssm/task/{device_id}/{task_id}                V2 编排任务
ssm/feedback/{session_id}                     V2 渐进式反馈
```

---

### Phone PWA（phone_ui）

**订阅：**
```
ssm/agents/+/manifest      ← AgentRegistry：发现设备
ssm/agents/+/status        ← AgentRegistry：在线状态
ssm/agents/+/location      ← AgentRegistry：设备坐标（距离排序）
ssm/agents/+/state         ← ISMTracker：实时状态
ssm/agents/+/event         ← ISMTracker：事件通知
ssm/agents/+/report        ← ISMTracker：传感器/执行器报告
ssm/feedback/{session_id}  ← ChatSheet：按需订阅，会话结束后移除
```

**发布：**
```
ssm/agents/phone_ui/manifest   [retain] 连接时自我声明（supervisor 类型）
ssm/intent/{session_id}               NLU 解析后携带 requirements 发布
```

---

## 九、Payload 格式参考

### manifest（设备能力声明）
```json
{
  "unit_id": "esp32_desk_led",
  "parent_id": "esp32_desk",
  "agent_type": "actuator",
  "name": "ws2812_ring",
  "hw_platform": "esp32",
  "firmware_ver": "0.2.0",
  "capabilities": [
    {"action": "SET_COLOR", "params": ["r", "g", "b", "brightness"]},
    {"action": "SET_STATE", "params": ["state"], "values": ["ON", "OFF", "BRIGHT", "DIM"]},
    {"action": "BLINK",    "params": ["r", "g", "b", "count"]}
  ],
  "resource_tags": ["lighting", "ambiance"],
  "agent_tag": "lighting",
  "ts": 1714200000
}
```

### ssm/intent/{session_id}
```json
{
  "session_id": "s_1714200000_a1b2c3",
  "user_msg": "感觉有点暗，我想看书",
  "requirements": [
    {"resource_tag": "lighting", "action": "set_color", "context": "reading"}
  ],
  "priority": 5,
  "ts": 1714200000
}
```

### ssm/feedback/{session_id}
```json
{
  "session_id": "s_1714200000_a1b2c3",
  "stage": "done",
  "text": "好了，灯已经调成暖光，适合阅读了。",
  "status": "ok",
  "ts": 1714200003
}
```
`stage` 枚举：`planning` | `executing` | `done` | `partial` | `failed`

### ssm/task/{device_id}/{task_id}
```json
{
  "task_id": "s_1714200000_a1b2c3_t0",
  "session_id": "s_1714200000_a1b2c3",
  "action": "SET_COLOR",
  "params": {"r": 255, "g": 220, "b": 150, "brightness": 200},
  "ts": 1714200002
}
```

### ssm/result/{device_id}/{task_id}
```json
{
  "task_id": "s_1714200000_a1b2c3_t0",
  "session_id": "s_1714200000_a1b2c3",
  "result": "ok",
  "ism_state": "COLOR",
  "ts": 1714200003
}
```
`result` 枚举：`ok` | `blocked` | `timeout`

### ssm/rules/{agent_id}（规则同步）
```json
[
  {"id": "r_1714200000_a1b2c3", "en": true,
   "trig": {"tag": "presence", "ev": "detected"},
   "act": {"cmd": "SET_STATE", "state": "BRIGHT"}},
  {"id": "r_1714200001_b2c3d4", "en": true,
   "trig": {"tag": "light_level", "ev": "dark"},
   "act": {"cmd": "SET_COLOR", "r": 255, "g": 160, "b": 60, "brightness": 180}}
]
```

### ssm/agents/{id}/command（V1 格式）
```json
{"cmd": "SET_COLOR", "r": 255, "g": 160, "b": 60, "brightness": 180}
{"cmd": "SET_STATE", "state": "OFF"}
{"cmd": "BLINK", "r": 255, "g": 255, "b": 255, "count": 2}
{"cmd": "PLAY", "pattern": "NOTIFY"}
```

---

## 十、硬件引脚

| 硬件 | GPIO | 备注 |
|------|------|------|
| WS2812 灯环（数据线） | GPIO 4 | 单总线，16 像素 |
| 蜂鸣器 | GPIO 5 | PWM，无源 |
| 声音传感器 | GPIO 15 | 数字输入，高=检测到 |
| 光线传感器 | GPIO 34 | ADC1，4 档（DARK/DIM/NORMAL/BRIGHT）|
| 红外传感器 | GPIO 19 | 数字输入，低=检测到 |
| GPIO 16/17 | — | 空闲 |

---

## 十一、网络地址

| 资源 | 地址 | 备注 |
|------|------|------|
| 云服务器公网 IP | `47.116.137.202` | |
| Broker TCP | `47.116.137.202:1883` | ESP32 直连 |
| Broker WebSocket | `47.116.137.202:9001` | PWA 本地调试直连 |
| nginx | `47.116.137.202:8080` | HTTP |
| ngrok 公网域名 | 动态（重启后变化）| 提供 HTTPS/WSS |
| PWA 访问地址 | `https://{ngrok域名}/` | 手机访问 |
| Chat API | `https://{ngrok域名}/api/` | → :8082 |
| MQTT over WSS | `wss://{ngrok域名}/mqtt` | → :9001 via nginx |

查询当前 ngrok 地址：
```bash
make ngrok-url
```

---

## 十二、代码结构

```
ssm/
├── broker/
│   ├── mosquitto.conf        # Broker 配置
│   └── passwd                # 认证用户（ssm_user）
├── agents/
│   ├── esp32/                # MicroPython，烧录到 ESP32
│   │   ├── main.py           # 主循环、MQTT 订阅注册
│   │   ├── config.py         # WiFi / MQTT / 引脚常量 + UNIT_CONFIGS 注册表
│   │   ├── probe.py          # 通用引脚探测引擎（读 UNIT_CONFIGS，零硬编码）
│   │   ├── ism.py            # 接口状态机（合法状态转换表）
│   │   ├── bsm.py            # 行为状态机（硬件驱动层）
│   │   ├── trigger_map.py    # ISM ↔ BSM ↔ MQTT 桥接（唯一耦合点）
│   │   ├── agent_manifest.py # 通用 manifest 发布（读 UNIT_CONFIGS）
│   │   ├── mqtt_client.py    # umqtt.robust 非阻塞封装
│   │   ├── local_rules.py    # 规则执行器（云端同步的规则 + flash 缓存）
│   │   └── rules_cache.json  # 规则 flash 缓存（运行时生成）
│   └── phone/                # PWA，部署到 :8081
│       ├── index.html
│       └── src/
│           ├── app.jsx           # React UI（雷达/设备/规则/对话四屏）
│           ├── MqttBus.js        # MQTT WebSocket 单例
│           ├── AgentRegistry.js  # 设备注册表（manifest/status/location）
│           └── ISMTracker.js     # 实时状态追踪（state/event/report）
├── server/
│   ├── .env                  # 云服务共用环境变量（API Key、MQTT、模型等）
│   ├── api/
│   │   └── main.py           # FastAPI：/api/nlu、/api/rules CRUD
│   └── orchestrator/
│       ├── main.py           # MQTT 事件循环：规则引擎 + V2 Orchestrator + V1 评估
│       ├── graph.py          # LangGraph：V1 评估图 + V2 Orchestrator
│       ├── rule_engine.py    # 规则引擎（热加载 rules.json，零 LLM）
│       ├── tools.py          # LangChain 工具函数
│       ├── shared_state.py   # 线程安全状态存储（能力注册表、任务结果）
│       └── rules.json        # 用户规则持久化（运行时生成）
├── Makefile                  # 统一服务管理
├── pyproject.toml            # uv 依赖管理
└── docs/
    └── ARCHITECTURE.md       # 本文件
```
