# SSM 系统架构文档

> 最后更新：2026-04-29
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
        │ (MicroPy)   │     │  server/orchestrator/│
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
| PC Agent | `make orchestrator` | — | 手动 |

---

## 二、设备列表

| 设备 ID | 类型 | 平台 | 代码路径 |
|---------|------|------|---------|
| `esp32_desk` | 父设备（ESP32 主机） | MicroPython | `agents/esp32/` |
| `esp32_desk_light` | 传感器 — 光线 | ESP32 子 unit | `agents/esp32/` |
| `esp32_desk_ir` | 传感器 — 红外 | ESP32 子 unit | `agents/esp32/` |
| `esp32_desk_sound` | 传感器 — 声音 | ESP32 子 unit | `agents/esp32/` |
| `esp32_desk_led` | 执行器 — WS2812 灯环 | ESP32 子 unit | `agents/esp32/` |
| `esp32_desk_buz` | 执行器 — 蜂鸣器 | ESP32 子 unit | `agents/esp32/` |
| `pc_decision` | PC 决策智能体 | Python / PC | `server/orchestrator/` |
| `phone_ui` | 手机监控 + 对话 | PWA / Browser | `agents/phone/` |

---

## 三、MQTT Topic 全表

### 3.1 标准 Agent 消息（所有设备遵循）

| Topic 模式 | 方向 | Retain | 说明 |
|-----------|------|--------|------|
| `ssm/agents/{unit_id}/manifest` | 设备 → Broker | ✅ | 设备能力声明，启动时发布 |
| `ssm/agents/{unit_id}/state` | 设备 → Broker | ✅ | 当前 ISM 状态，变更时发布 |
| `ssm/agents/{unit_id}/event` | 设备 → Broker | ❌ | 状态跳变通知（传感器）|
| `ssm/agents/{unit_id}/report` | 设备 → Broker | ❌ | 传感器读数 / 执行器反馈 |
| `ssm/agents/{unit_id}/status` | 设备 → Broker | ✅ | `online` / `offline`（Last Will）|
| `ssm/agents/{unit_id}/location` | 设备 → Broker | ✅ | GCJ-02 坐标，启动时发布 |

### 3.2 控制指令（V1 路径）

| Topic | 方向 | Retain | 说明 |
|-------|------|--------|------|
| `ssm/agents/{unit_id}/command` | PC Agent → ESP32 | ❌ | V1 直接指令（SET_COLOR / BLINK 等）|
| `ssm/decision/active` | PC Agent → 全体 | ✅ | `"true"` = PC 接管，ESP32 本地规则停用 |
| `ssm/decision/evaluation` | PC Agent → Broker | ❌ | 执行效果评估结论 |

### 3.3 意图 + 任务（V2 路径）

| Topic | 方向 | Retain | 说明 |
|-------|------|--------|------|
| `ssm/intent/{session_id}` | Phone → Broker | ❌ | 用户自然语言意图（NLU 解析后）|
| `ssm/feedback/{session_id}` | PC Agent → Phone | ❌ | 渐进式反馈（planning/executing/done 等）|
| `ssm/task/{device_id}/{task_id}` | PC Agent → ESP32 | ❌ | 编排任务（action + params）|
| `ssm/result/{device_id}/{task_id}` | ESP32 → PC Agent | ❌ | 任务执行结果（ok/blocked/timeout）|

### 3.4 系统维护

| Topic | 方向 | 说明 |
|-------|------|------|
| `ssm/sys/ping` | 任意 → ESP32 | Ping 检测 |
| `ssm/sys/pong/{unit_id}` | ESP32 → 任意 | Pong 响应 |
| `ssm/agents/{esp32_desk}/heartbeat` | ESP32 → Broker | 每 60s 心跳 |
| `ssm/sys/phone_will` | Phone → Broker | PWA Last Will（`offline`）|

---

## 四、各设备发布 / 订阅详表

### ESP32（esp32_desk）

**订阅：**
```
ssm/agents/esp32_desk_led/command      ← V1 LED 控制指令
ssm/agents/esp32_desk_buz/command      ← V1 蜂鸣器控制指令
ssm/task/esp32_desk_led/+              ← V2 LED 任务
ssm/task/esp32_desk_buz/+              ← V2 蜂鸣器任务
ssm/decision/active                    ← PC Agent 接管标志
ssm/sys/ping                           ← 系统 Ping
```

**发布：**
```
ssm/agents/esp32_desk/status           [retain] online / offline (Last Will)
ssm/agents/esp32_desk/location         [retain] 固定坐标 GCJ-02
ssm/agents/esp32_desk/heartbeat               每 60s，含 light/ir 快照

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
ssm/agents/esp32_desk_led/report              V1 command 执行反馈 {cmd, result, ism_state}

ssm/agents/esp32_desk_buz/manifest     [retain]
ssm/agents/esp32_desk_buz/state        [retain] {ism: SILENT|ALERT|NOTIFY}
ssm/agents/esp32_desk_buz/report              V1 command 执行反馈 {cmd, result, ism_state}

ssm/result/esp32_desk_led/{task_id}           V2 任务执行结果
ssm/result/esp32_desk_buz/{task_id}           V2 任务执行结果

ssm/sys/pong/esp32_desk_led                   Ping 响应
```

---

### PC Agent（pc_decision）

**订阅：**
```
ssm/agents/+/manifest      ← 发现所有设备，构建能力注册表
ssm/agents/+/state         ← 跟踪所有 ISM 状态
ssm/agents/+/event         ← 传感器事件 → 触发 V1 Decision Agent
ssm/agents/+/report        ← 执行器反馈 → 触发 V1 Evaluation Agent
ssm/decision/active        ← 监听自身控制标志（防多实例冲突）
ssm/intent/+               ← 手机意图 → 触发 V2 Orchestrator
ssm/result/+/+             ← V2 任务结果 → 写入 SharedState 供 Evaluator 读取
```

**发布：**
```
ssm/decision/active                    [retain] "true" 上线 / "false" 下线
ssm/agents/pc_decision/manifest               自身能力声明
ssm/agents/pc_decision/state                  ACTIVE 状态

ssm/agents/{unit_id}/command                  V1 直接控制指令（LED / 蜂鸣器）
ssm/decision/evaluation                       V1 执行评估结论

ssm/task/{device_id}/{task_id}                V2 编排任务
ssm/feedback/{session_id}                     V2 渐进式反馈（nlu_done/planning/executing/done/partial/failed）
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
ssm/intent/{session_id}               用户发送消息后，携带 NLU 解析结果
ssm/sys/phone_will             [LWT]  "offline"（连接断开时 broker 自动发布）
```

---

## 五、两条决策路径对比

| 维度 | V1 传感器自动响应 | V2 用户意图主动控制 |
|------|----------------|-------------------|
| **触发源** | ESP32 传感器事件（`ssm/agents/+/event`）| 手机用户自然语言输入 |
| **入口** | PC Agent `event_queue` trigger=sensor | `ssm/intent/{session_id}` |
| **决策组件** | `decision_react`（ReAct Agent）| `planner_node`（LLM + 能力注册表）|
| **下发指令** | `ssm/agents/{id}/command` | `ssm/task/{device_id}/{task_id}` |
| **反馈路径** | `ssm/agents/{id}/report` → evaluation_node | `ssm/result/{device_id}/{task_id}` → evaluator_node → `ssm/feedback/{session_id}` |
| **用户可见** | 否（静默执行）| 是（Chat 渐进式反馈）|
| **特点** | 环境感知、被动响应 | 用户主动、有会话上下文 |
| **冲突协调** | `ssm/decision/active` flag（V1 可被抑制）| 无（当前两路独立，ISM 做最终仲裁）|

### V2 完整链路时序

```
手机输入文字
  │
  ├─ POST /api/nlu → {session_id, nlu_feedback, requirements}
  │
  ├─ 订阅 ssm/feedback/{session_id}
  ├─ 发布 ssm/intent/{session_id}
  │
  │  [PC Agent Orchestrator]
  ├─ Planner   → 查能力注册表 + LLM 推理 → planned_tasks
  │            → 发布 ssm/feedback/{sid} stage=planning
  ├─ Dispatcher → 发布 ssm/task/{device_id}/{task_id}
  │            → 发布 ssm/feedback/{sid} stage=executing
  ├─ Evaluator → 等待 ssm/result/{device_id}/{task_id}（最多 6s）
  ├─ Responder → LLM 生成自然语言
  │            → 发布 ssm/feedback/{sid} stage=done|partial|failed
  │
  └─ 手机 ChatSheet 收到 feedback，渐进展示
```

---

## 六、Payload 格式参考

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
  "ts": 1714200000
}
```

### ssm/intent/{session_id}
```json
{
  "session_id": "s_1714200000_a1b2c3",
  "user_msg": "感觉有点暗，我想看书",
  "requirements": [
    {"resource_tag": "lighting", "action": "brighten", "context": "reading"}
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
  "text": "好了，灯已经调亮，适合阅读了。",
  "status": "ok",
  "ts": 1714200003
}
```
`stage` 枚举：`nlu_done` | `planning` | `executing` | `done` | `partial` | `failed`

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
`result` 枚举：`ok` | `blocked` | `timeout` | `error`

### ssm/agents/{id}/command（V1 格式）
```json
{"cmd": "SET_COLOR", "r": 255, "g": 160, "b": 60, "brightness": 180}
{"cmd": "SET_STATE", "state": "OFF"}
{"cmd": "BLINK", "r": 255, "g": 255, "b": 255, "count": 2}
{"cmd": "PLAY", "pattern": "NOTIFY"}
```

---

## 七、硬件引脚

| 硬件 | GPIO | 备注 |
|------|------|------|
| WS2812 灯环（数据线） | GPIO 4 | 单总线，16 像素 |
| 蜂鸣器 | GPIO 5 | PWM，无源 |
| 声音传感器 | GPIO 15 | 数字输入，高=检测到 |
| 光线传感器 | GPIO 18 | 数字输入（无 ADC），仅亮/暗两档 |
| 红外传感器 | GPIO 19 | 数字输入，低=检测到 |

---

## 八、网络地址

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
curl -s http://localhost:4040/api/tunnels | python3 -c \
  "import sys,json;[print(t['public_url']) for t in json.load(sys.stdin)['tunnels']]"
```

---

## 九、代码结构

```
ssm/
├── broker/
│   ├── mosquitto.conf        # Broker 配置
│   └── passwd                # 认证用户（ssm_user）
├── agents/
│   ├── esp32/                # MicroPython，烧录到 ESP32
│   │   ├── main.py           # 主循环、MQTT 订阅注册
│   │   ├── config.py         # WiFi / MQTT / 引脚常量
│   │   ├── ism.py            # 接口状态机（合法状态转换表）
│   │   ├── bsm.py            # 行为状态机（硬件驱动层）
│   │   ├── trigger_map.py    # ISM ↔ BSM ↔ MQTT 桥接（唯一耦合点）
│   │   ├── agent_manifest.py # 启动时发布 5 个 unit 的 manifest
│   │   ├── mqtt_client.py    # umqtt.robust 非阻塞封装
│   │   └── local_rules.py    # 本地自动规则（PC 离线时启用）
│   └── phone/                # PWA，部署到 :8081
│       ├── index.html
│       └── src/
│           ├── app.jsx           # React UI（发现、设备、对话三屏）
│           ├── MqttBus.js        # MQTT WebSocket 单例
│           ├── AgentRegistry.js  # 设备注册表（manifest/status/location）
│           └── ISMTracker.js     # 实时状态追踪（state/event/report）
├── server/
│   ├── .env                  # 云服务共用环境变量（API Key、MQTT、模型等）
│   ├── api/
│   │   └── main.py           # FastAPI：/api/chat（V1）、/api/nlu（V2）
│   └── orchestrator/
│       ├── main.py           # MQTT 事件循环，路由到两条图
│       ├── graph.py          # LangGraph：V1 决策图 + V2 Orchestrator
│       ├── tools.py          # LangChain @tool 函数（传感器读取、指令发布等）
│       └── shared_state.py   # 线程安全状态存储（能力注册表、任务结果）
├── Makefile                  # 统一服务管理（make broker/api/orchestrator/pwa/ngrok）
├── pyproject.toml            # uv 依赖管理
└── docs/
    ├── ARCHITECTURE.md       # 本文件
    └── ARCHITECTURE_V2.md    # V2 设计规划原稿
```
