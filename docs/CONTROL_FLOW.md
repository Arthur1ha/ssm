# SSM 控制链路全览

> 本文档描述用户输入如何流经各层智能体，最终驱动端侧设备执行，以及结果如何回传。

## 目录

1. [系统分层架构](#系统分层架构)
2. [两条决策路径](#两条决策路径)
3. [V1：传感器自动响应](#v1传感器自动响应)
4. [V2：用户意图驱动](#v2用户意图驱动)
5. [MQTT Topic 完整映射](#mqtt-topic-完整映射)
6. [ESP32 端状态机体系](#esp32-端状态机体系)
7. [关键组件职责](#关键组件职责)
8. [系统启动顺序](#系统启动顺序)
9. [异常处理机制](#异常处理机制)

---

## 系统分层架构

```
手机 PWA（用户界面 + MQTT 订阅者）
    │
    ├── HTTPS ──→ FastAPI /api/nlu（云端 NLU 解析）
    │
    └── WSS ─────→ nginx :9001
                       │
                       ▼
              MQTT Broker（Mosquitto）
              TCP :1883  WS :9001
                       │
              ┌────────┴────────┐
              ▼                 ▼
        PC 决策智能体       ESP32 边缘智能体
        (LangGraph)         (MicroPython)
        订阅 MQTT 事件      订阅命令/任务 topic
        发布控制指令        发布传感器数据/执行结果
```

---

## 两条决策路径

| 路径 | 触发来源 | 入口 | 决策引擎 | 典型场景 |
|------|---------|------|---------|---------|
| **V1** | 传感器事件 / 执行器反馈 | ESP32 → MQTT event | LangGraph 双节点（Decision + Evaluation） | 光线变暗自动开灯 |
| **V2** | 用户自然语言输入 | 手机 → POST /api/nlu → MQTT intent | LangGraph 四节点（Planner → Dispatcher → Evaluator → Responder） | "帮我把灯调暗一点" |

---

## V1：传感器自动响应

### 链路流程

```
ESP32 BSM.tick()
  → 传感器数值变化（GPIO 读取）
  → event_cb("LIGHT_CHANGED" / "IR_TRIGGERED", data)
  → TriggerMap.on_bsm_event()
  → ISM 状态转换
  → 发布 3 条 MQTT 消息：
      ssm/agents/{id}/state  [retain]
      ssm/agents/{id}/event
      ssm/agents/{id}/report

PC Agent on_message()（main.py）
  → suffix=="light" / "ir" → event_queue.put(trigger="sensor")
  → graph.invoke({"trigger": "sensor", "payload": {...}})

LangGraph Router
  → trigger=="sensor" → decision_node
  → trigger=="actuator" → evaluation_node
```

### decision_node（ReAct Agent）

工具调用顺序：
1. `get_sensor_snapshot()` — 读 SharedState._sensors，获取所有传感器当前快照
2. `get_capabilities()` — 读 SharedState._capability_registry，获取设备能力与 tag
3. `publish_led_command(cmd, r, g, b, brightness)` — 向 LED 发布 V1 指令
4. `publish_buzzer_command(mode)` — 向蜂鸣器发布 V1 指令

发布 topic：`ssm/agents/{led_id}/command`
```json
{"cmd": "SET_COLOR", "r": 255, "g": 160, "b": 60, "brightness": 180, "ts": 1714200001}
```

### evaluation_node（ReAct Agent）

在收到执行器 report 后触发，工具调用：
1. `get_last_decision()` — 读上次决策内容
2. `get_actuator_snapshot()` — 读 _actuators 最新 report
3. `publish_assessment(result, reason)` — 发布评估结论

发布 topic：`ssm/decision/evaluation`
```json
{"result": "ok", "reason": "LED 已成功切换到暖色", "ts": 1714200005}
```

### V1 完整数据流

```
1. ESP32 检测光线 → 发布 event
2. PC Agent 收到 event → decision_node
3. LLM 调用工具 → 发布 LED command
4. ESP32 执行命令 → 发布 report
5. PC Agent 收到 report → evaluation_node
6. LLM 评估执行结果 → 发布 assessment
```

---

## V2：用户意图驱动

### 完整链路

```
用户输入："帮我把灯调暗一点"
    │
    ▼ POST /api/nlu
FastAPI main.py（server/api/main.py）
  LLM (hy3-preview) 解析：
  → {nlu_feedback, requirements, session_id}
  ← 返回给手机端
    │
    ▼ 手机端
  1. 订阅 ssm/feedback/{session_id}
  2. 发布 ssm/intent/{session_id}
     {user_msg, requirements, session_id}
    │
    ▼ MQTT Broker → PC Agent
PC Agent on_message()
  parts[1]=="intent" → event_queue.put(trigger="intent", session_id, payload)
  orchestrator.invoke(OrchestratorState)
    │
    ├─ Planner Node
    ├─ Dispatcher Node
    ├─ Evaluator Node
    └─ Responder Node
         │
         ▼ ssm/feedback/{session_id}
    手机 ChatSheet 渐进显示
```

### Planner Node

```python
# 1. 读能力注册表
registry = _state.get_capability_registry()
# {"lighting": ["esp32_desk_led"], "ambiance": ["esp32_desk_led"]}

# 2. 构建 device_str（每个设备的 action/params 列表）

# 3. LLM 推理 → JSON 数组
planned_tasks = [
    {"device_id": "esp32_desk_led", "task_id": "..._t0",
     "action": "SET_STATE", "params": {"state": "DIM"}}
]

# 4. 发布渐进反馈
do_publish_feedback(sid, "planning", "正在规划控制方案...")
```

早退条件：capability_registry 为空 → feedback(failed) → early_exit=True

### Dispatcher Node

```python
for task in planned_tasks:
    do_publish_task(device_id, task_id, action, params, session_id)
    # 发布 ssm/task/{device_id}/{task_id}
    # {"task_id", "session_id", "action", "params", "ts"}

do_publish_feedback(sid, "executing", "正在控制设备...")
```

早退条件：planned_tasks 为空 → feedback(failed) → early_exit=True

### ESP32 任务处理

```
trigger_map.on_mqtt(topic="ssm/task/esp32_desk_led/{task_id}", payload)
  → _handle_led_task(payload, task_id)
  → BSM.led_set_state("DIM")  # 硬件操作
  → ISM_LED.transition(CMD_DIM)  # 状态转换
  → 发布 ssm/result/esp32_desk_led/{task_id}
    {"task_id", "session_id", "result": "ok", "ism_state": "DIM", "ts"}
```

ISM 转换被阻止时 result="blocked"。

### Evaluator Node

```python
deadline = now + 6.0  # 等待最多 6 秒
while time < deadline:
    for task in planned_tasks:
        r = _state.get_task_result(task_id)
        if r: results[task_id] = r
    if all tasks collected: break
    sleep(0.2)

# 超时任务补默认结果
for task in planned_tasks:
    if task_id not in results:
        results[task_id] = {"result": "timeout", "task_id": task_id}
```

### Responder Node

```python
ok_count = sum(1 for r in results.values() if r["result"] == "ok")
stage = "done" if ok_count == len(results) else \
        "partial" if ok_count > 0 else "failed"

# LLM 生成自然语言，1 句，不提技术细节
response_text = llm(user_msg + task_results)

do_publish_feedback(sid, stage, response_text)
# 发布 ssm/feedback/{session_id}
# {"session_id", "stage", "text", "status", "ts"}
```

### OrchestratorState 字段演化

| 阶段 | 新增/更新字段 |
|------|-------------|
| 初始 | session_id, user_msg, requirements, planned_tasks=[], task_results={}, early_exit=False |
| Planner 后 | planned_tasks=[...] |
| Dispatcher 后 | （任务已发出，state 不变） |
| Evaluator 后 | task_results={task_id: result} |
| Responder 后 | response_text="..." |

---

## MQTT Topic 完整映射

### 标准 Agent 消息（四类 + location）

| Topic | 发布者 | Retain | 说明 |
|-------|--------|--------|------|
| `ssm/agents/{id}/manifest` | 所有设备（启动时） | ✅ | 能力声明（tags, actions, params） |
| `ssm/agents/{id}/state` | 设备（状态变更时） | ✅ | ISM 当前状态 |
| `ssm/agents/{id}/event` | 设备（状态跳变时） | ❌ | 变更通知 |
| `ssm/agents/{id}/report` | 设备（传感器/执行器） | ❌ | 传感器观测值或执行反馈 |
| `ssm/agents/{id}/location` | 设备（启动时） | ✅ | GCJ-02 坐标，手机计算距离用 |

### V1 控制

| Topic | 发布者 | 订阅者 | 说明 |
|-------|--------|--------|------|
| `ssm/agents/{id}/command` | PC decision_node | ESP32 | V1 直接指令（SET_COLOR/BLINK 等） |
| `ssm/decision/active` | PC Agent | ESP32 local_rules | "true"=PC 接管，"false"=本地规则 |
| `ssm/decision/evaluation` | PC evaluation_node | （记录用） | 执行评估结论 |

### V2 编排

| Topic | 发布者 | 订阅者 | 说明 |
|-------|--------|--------|------|
| `ssm/intent/{session_id}` | 手机 | PC Orchestrator | 用户意图 + 结构化需求 |
| `ssm/task/{device_id}/{task_id}` | PC Dispatcher | ESP32 | 编排任务（action+params） |
| `ssm/result/{device_id}/{task_id}` | ESP32 | PC Evaluator | 任务执行结果 |
| `ssm/feedback/{session_id}` | PC Responder | 手机 ChatSheet | 渐进反馈（stage: planning/executing/done/partial/failed） |

### ESP32 订阅清单（trigger_map.py）

```
ssm/agents/{AGENT_LED}/command      # V1 LED 指令
ssm/agents/{AGENT_BUZ}/command      # V1 蜂鸣器指令
ssm/task/{AGENT_LED}/+              # V2 LED 任务
ssm/task/{AGENT_BUZ}/+              # V2 蜂鸣器任务
ssm/decision/active                 # 控制权标志（local_rules 监听）
ssm/sys/ping                        # 系统心跳
```

---

## ESP32 端状态机体系

### 分层职责

| 层 | 文件 | 职责 | 依赖 |
|----|------|------|------|
| BSM（行为） | bsm.py | GPIO/PWM/ADC 驱动，读传感器，触发回调 | 硬件引脚 |
| ISM（接口） | ism.py | 合法状态转换表，纯数据结构，无副作用 | 无 |
| TriggerMap（桥接） | trigger_map.py | MQTT ↔ ISM ↔ BSM 唯一耦合点 | BSM + ISM + MQTT |

### ISM 状态转换（LED）

```
OFF
  ├─ CMD_ON    → BRIGHT
  ├─ CMD_DIM   → DIM
  ├─ CMD_COLOR → COLOR
  └─ CMD_BLINK → BLINK

BRIGHT / DIM / COLOR
  ├─ CMD_OFF        → OFF
  ├─ CMD_BRIGHT     → BRIGHT
  ├─ CMD_DIM        → DIM
  ├─ CMD_COLOR      → COLOR
  └─ CMD_BLINK      → BLINK

BLINK
  ├─ BLINK_DONE → OFF
  └─ CMD_OFF    → OFF
```

### ISM 状态转换（蜂鸣器）

```
SILENT
  ├─ PLAY_NOTIFY → NOTIFY
  └─ PLAY_ALERT  → ALERT

NOTIFY / ALERT
  ├─ SOUND_DONE  → SILENT
  └─ STOP_SOUND  → SILENT
```

### ISM 状态转换（光线/红外传感器）

```
BOOT
  └─ INIT_COMPLETE → SAMPLING / MONITORING

SAMPLING / MONITORING
  ├─ SENSOR_FAIL → ERROR
  └─ HW_FAULT    → ERROR

ERROR
  ├─ SENSOR_RECOVERED → SAMPLING
  └─ CMD_RESET        → SAMPLING
```

---

## 关键组件职责

### server/api/main.py

- `/api/chat`（V1）：直接调用 LLM，返回 `{reply, commands}`
- `/api/nlu`（V2）：解析用户意图，返回 `{session_id, nlu_feedback, requirements}`
- LLM：hy3-preview（`CHAT_API_BASE_URL` + `CHAT_MODEL`）

### server/orchestrator/main.py

- 连接 MQTT Broker，订阅 `ssm/#`
- `on_message()` 按 topic suffix 分类，写入 `event_queue`
- 主循环：`event_queue.get()` → 触发 `graph.invoke()` 或 `orchestrator.invoke()`
- 同时维护 SharedState（manifest/state/event/report/result 入库）

### server/orchestrator/shared_state.py

线程安全存储，关键方法：

| 方法 | 说明 |
|------|------|
| `register_capability(manifest)` | manifest 到达时注册设备能力 |
| `get_capability_registry()` | 按 tag 返回设备列表 |
| `update_sensor(id, data)` | 更新传感器快照 |
| `get_sensor_snapshot()` | 获取全部传感器当前值 |
| `update_actuator(id, data)` | 更新执行器快照 |
| `set_last_decision(data)` | 保存上次 V1 决策 |
| `store_task_result(task_id, result)` | 存 V2 任务结果 |
| `get_task_result(task_id)` | 读 V2 任务结果（Evaluator 轮询用） |

### agents/phone/src/

| 文件 | 职责 |
|------|------|
| `MqttBus.js` | WebSocket MQTT 单例连接，emit 事件分发 |
| `AgentRegistry.js` | 管理设备列表，解析 manifest/location，按距离排序 |
| `ISMTracker.js` | 实时跟踪设备 ISM 状态 |
| `DecisionAgent.js` | 发布 `ssm/decision/active=false`（手机不抢控） |
| `ChatSheet.js` | 订阅 feedback topic，渐进显示规划/执行/完成状态 |

---

## 系统启动顺序

```bash
# 云服务器端
make broker       # Mosquitto 后台守护（:1883 TCP + :9001 WS）
make api-bg       # FastAPI :8082，日志 → /tmp/ssm_api.log
make pwa-bg       # PWA 静态服务 :8081
make ngrok-bg     # 公网 HTTPS 隧道（重启后地址变）
make orchestrator # PC Agent 前台，监听 MQTT 事件

# ESP32 端（上电自动运行）
# boot.py → WiFi 连接
# main.py → BSM/ISM/TriggerMap 初始化 → MQTT 连接 → manifest 发布 → 主循环
```

---

## 异常处理机制

### ESP32 任务执行失败

- ISM 转换被阻止：`result="blocked"`，附当前 `ism_state`
- 硬件异常：ISM 进入 ERROR 状态，发布 state 变更通知

### Evaluator 超时（6 秒）

```
未收到 result → result="timeout"
Responder 阶段：ok_count < len(results) → stage="partial" 或 "failed"
```

### Planner/Dispatcher 早退

| 条件 | 反馈 |
|------|------|
| capability_registry 为空 | feedback(failed, "没有发现附近设备") |
| planned_tasks 为空 | feedback(failed, "没找到合适设备") |
| LLM JSON 解析失败 | tasks_raw=[]，后续 Dispatcher 触发早退 |

### 本地规则兜底（离线）

`ssm/decision/active=false` 时，ESP32 `local_rules.py` 接管：
- 光线变暗 → 自动点亮 LED（不依赖 PC Agent）

---

## 环境变量（server/.env）

| 变量 | 说明 |
|------|------|
| `OPENAI_API_KEY` / `OPENAI_BASE_URL` | PC Orchestrator LLM（豆包） |
| `MODEL` | PC 决策模型 |
| `MQTT_BROKER_HOST` / `MQTT_BROKER_PORT` | Mosquitto 地址 |
| `PC_AGENT_ID` | PC 智能体 MQTT 客户端 ID |
| `CHAT_API_BASE_URL` / `CHAT_API_KEY` / `CHAT_MODEL` | Chat API LLM（hy3-preview，NLU 解析用） |
