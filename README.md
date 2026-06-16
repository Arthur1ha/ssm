# SSM — 智能系统网格（Smart System Mesh）

无中心的分布式多智能体 IoT 系统：以 MQTT 为统一**控制总线**（发现、在线状态、能力声明 card），**数据面按设备本性选传输**（ESP32 走 MQTT、Go2 走 HTTP/SSE）。连接 ESP32 边缘设备、云端 AI 智能体（编排器 / ESP32 桌面智能体 / Go2 机器狗）和手机 PWA 控制界面。新智能体通过 A2A Agent Card 暴露能力即可被动态发现与调用。

## 系统架构

```
┌─────────────────────────────────────────────────────────────┐
│                       MQTT 消息代理                          │
│                   Mosquitto :1883 / :9001                    │
└──────────┬────────────────────────────┬─────────────────────┘
           │                            │
  ┌────────▼─────────┐        ┌─────────▼──────────────────────┐
  │  ESP32（边缘端）  │        │       云端服务（cloud/）         │
  │  MicroPython     │        │  ┌──────────────────────────┐  │
  │                  │        │  │ orchestrator 编排器       │  │
  │  BSM ← TriggerMap│        │  │ Planner→Dispatcher→       │  │
  │  ISM ← TriggerMap│        │  │ Evaluator→Responder       │  │
  │  本地规则（兜底） │        │  ├──────────────────────────┤  │
  └────────┬─────────┘        │  │ api（FastAPI :8082）      │  │
           │           HTTP   │  │  esp32 桌面智能体          │  │
           │        ◄────────►│  │  go2 机器狗智能体（WebRTC）│  │
           │                  │  └──────────────────────────┘  │
  ┌────────▼──────────────────────────────────────────────────┤
  │              手机 PWA（发现 + 监控 + 对话）                  │
  │        WebSocket MQTT + REST，自然语言控制                  │
  └─────────────────────────────────────────────────────────────┘
```

**控制优先级**：用户指令 > 智能体 > 本地规则（离线兜底）

云端有三个 LLM 智能体：
- **编排器**（`cloud/orchestrator/`，独立进程）：订阅用户意图，经 LangGraph 图统筹全局，MQTT 派发到 ESP32、HTTP 委托到 Go2。
- **ESP32 桌面智能体**（`cloud/esp32/agent.py`）：带 persona 的 sense→reason→act，自主响应传感器事件。
- **Go2 机器狗智能体**（`cloud/go2/`）：独立 LangGraph，含性格演化、记忆、导航、视觉，经 HTTP 暴露 `/api/go2/*`。

## esp硬件引脚

| 硬件 | GPIO | 模式 |
|------|------|------|
| WS2812 灯环（数据线） | GPIO4 | 单总线 |
| 光线传感器 | GPIO34 | 模拟（ADC1） |
| 声音传感器 | GPIO15 | 数字，上升沿触发 |

传感器是否在线由 `edge/probe.py` 在启动时自动探测（拉电阻技巧），无需手动配置开关。具体接线以 `edge/config.py` 的 `UNIT_CONFIGS` 为准。

## 各组件说明

### ESP32 边缘端（`edge/`）

| 文件 | 职责 |
|------|------|
| `boot.py` | 启动时连接 WiFi |
| `config.py` | 引脚、MQTT 地址、时序常量、**UNIT_CONFIGS**（unit 注册表） |
| `probe.py` | 启动时自动探测各引脚是否有传感器接线 |
| `bsm.py` | 行为状态机——GPIO/PWM/ADC 驱动，事件回调 |
| `ism.py` | 接口状态机——仅处理状态转换，不涉及硬件 |
| `trigger_map.py` | BSM ↔ ISM ↔ MQTT 接线（唯一耦合点） |
| `agent_manifest.py` | 启动时为已探测到的 unit 发布 retained manifest |
| `local_rules.py` | 云端智能体离线时的本地自动规则 |
| `mqtt_client.py` | MQTT 封装，支持自动重连和 LWT |
| `main.py` | 主循环入口 |

**本地规则**（`ssm/decision/active = "false"` 时启用）：
- 光线 DARK/DIM → 暖白光 LED（R=255, G=160, B=60）
- 光线 BRIGHT → 关灯
- 检测到声音 → 白色闪烁 ×2（始终生效）

### 云端服务（`cloud/`）

环境变量统一存放在 `cloud/.env`（见 `cloud/.env.example`）。编排器 / Go2 / ESP32 智能体均用火山方舟 Ark `deepseek-v4-*`（`MODEL_LIST`）。

#### API（`cloud/api/`，FastAPI，端口 8082）

| 接口 | 说明 |
|------|------|
| `GET  /api/devices` | 列出所有在线设备（`devices.py`） |
| `GET  /api/devices/{unit_id}/agent` | **A2A Agent Card**：机器可读的能力描述（数据取自 `cloud/cards/`） |
| `GET/POST/DELETE /api/rules` | 自动化规则 CRUD（`rules.py`） |
| `/api/go2/*` | Go2 连接 / 运动 / 导航 / 视觉 / 对话（`cloud/go2/router.py`） |
| `GET/PUT /api/esp32/autonomy` | ESP32 灯智能体自主模式（manual/reactive，`cloud/esp32/router.py`） |

`api/main.py` 同时承载一个 ESP32 MQTT 桥，订阅 `manifest/state/event/result` 并喂给 ESP32 桌面智能体。

#### 编排器（`cloud/orchestrator/`，独立进程）

Python + LangGraph，订阅 `ssm/intent/+`，驱动编排图。

| 文件 | 职责 |
|------|------|
| `main.py` | MQTT 事件循环：意图 → 编排图 |
| `graph.py` | LangGraph：Planner→Dispatcher→Evaluator→Responder（含多模型 fallback） |
| `tools.py` | MQTT/HTTP 派发与反馈辅助 |
| `shared_state.py` | 线程安全设备/任务快照（合并 MQTT manifest + `devices.json`） |
| `devices.json` | 预留：文件型非 MQTT 设备注册表（当前为空 `{}`，go2 已走 MQTT card） |
| `rules.json` | 自动化规则存储 |

> 设备能力卡注册表见 `cloud/cards/`（`registry.py` 订阅 card/manifest/status，是 A2A 单一真相）。

### 手机 PWA（`app/`）

无构建步骤的 React 应用（Babel standalone），通过 WebSocket 连接 MQTT 代理（端口 9001）。自动订阅 `ssm/agents/#`，实时接收所有设备状态。

界面形态：**单屏聊天中心 + hash 子页**——主屏自上而下为「设备卡片列表（`DevicesScreen`/`DeviceCard`）→ 活动流气泡（`ActivityFeed`）→ 底部输入栏」，子页经 hash 路由（`#go2` / `#<unit_id>`）。功能：设备自动发现、传感器实时数据、执行器控制、自然语言对话、自动化规则管理（`RulesDrawer`）、Go2 专属控制页。源码分层：`pages/`、`components/`、`hooks/`、`utils/`（agentMeta/audio），MQTT 统一经 `MqttBus` 单例。

## MQTT 消息协议

**完整定义以 `protocol/topics.md` 为单一真实来源**，下表为概览：

| 类型 | Topic | 保留 | 发布者 | 说明 |
|------|-------|------|--------|------|
| `manifest` | `ssm/agents/{unit_id}/manifest` | 是 | ESP32 | 设备能力声明，启动时发布；空 payload = 单元缺席移除 |
| `card` | `ssm/agents/{unit_id}/card` | 是 | Go2 | 完整 AgentCard JSON（自描述设备） |
| `state` | `ssm/agents/{unit_id}/state` | 是 | ESP32 | 当前 ISM 状态，变更时发布 |
| `event` | `ssm/agents/{unit_id}/event` | 否 | ESP32 | 传感器事件 |
| `status` | `ssm/agents/{device_id}/status` | 是 | ESP32 / Go2 | `online`/`offline`（掉线由 LWT 自动置 offline）|
| `location` | `ssm/agents/{device_id}/location` | 是 | ESP32 | 设备地理坐标（GCJ-02） |

**控制 / 编排 / 表达 topic**：
- `ssm/intent/{session_id}` —— 手机意图（PWA 发布，编排器订阅）
- `ssm/task/{unit_id}/{task_id}` —— 任务下发（云端 → 设备）
- `ssm/result/{unit_id}/{task_id}` —— 任务结果（设备 → 云端，`ok`/`blocked`）
- `ssm/feedback/{session_id}` —— 编排器渐进式执行反馈
- `ssm/decision/active` —— 云端在线总开关（`true`/`false`，离线时 ESP32 本地规则接管）
- `ssm/rules/{device_id}` —— 自动化规则（retained）
- `ssm/agents/{unit_id}/thought`、`ssm/agents/esp32_desk_led/speech` —— 拟人化思考 / 语音表达

所有 payload 均为 JSON，每条消息带 `unit_id`（自标识键）和 `ts`（Unix 时间戳）。

## 快速上手

### 0. Python 环境

```bash
uv sync          # 安装所有依赖
uv add <包名>    # 添加新依赖
```

始终在项目根目录执行 uv 命令；运行脚本用 `uv run python`，无需激活虚拟环境。

### 1. 启动云端服务

```bash
make broker        # 启动 MQTT Broker（前台，tail 日志）
make api           # 前台启动 FastAPI（端口 8082，--reload）
make orchestrator  # 前台启动编排器（崩溃自动重启）
make pwa-bg        # 后台启动 PWA 文件服务（端口 8081）
make tunnel-bg     # 后台启动 Cloudflare Tunnel HTTPS 隧道
make tunnel-url    # 输出公网地址（https://ssm.eliottxu.top）
make ps / logs     # 查看进程 / 日志
```

> 服务建议在 tmux 窗口中前台运行，便于观察日志；带 `-bg` 的目标为后台幂等启动。

### 2. 配置并上传 ESP32

编辑 `edge/config.py`，填入 WiFi 账号和 Broker 地址，通过 [mpremote](https://docs.micropython.org/en/latest/reference/mpremote.html) 上传：

```bash
cd edge
mpremote connect <端口> cp boot.py config.py probe.py ism.py bsm.py \
  mqtt_client.py trigger_map.py local_rules.py agent_manifest.py main.py :
mpremote connect <端口> reset
```

上传后串口应输出 `[Probe] {...}` 显示各传感器探测结果。

### 3. 打开手机 PWA

浏览器访问 `https://ssm.eliottxu.top`，即可看到自动发现的设备卡片列表,并通过底部输入栏自然语言操作。

## 网络拓扑

```
手机浏览器（HTTPS）
    │
    ▼
Cloudflare Tunnel（https://ssm.eliottxu.top）  ← 提供 HTTPS/WSS
    │
    ▼ HTTP（端口 8080）
nginx
    ├── /        → PWA 静态文件（端口 8081）
    ├── /api     → FastAPI（端口 8082）
    └── /mqtt    → Mosquitto WebSocket（端口 9001）

ESP32
    └── TCP 1883 → Mosquitto（直连，无需 TLS）
```

PWA 内 MQTT 地址自动切换：HTTPS 访问时用 `wss://ssm.eliottxu.top/mqtt`，HTTP 访问时直连 `ws://47.116.137.202:9001`。

## 数据流示例

**传感器自动响应：**
```
光线变暗
  → BSM 检测 GPIO34 ADC 值下降 → event_cb("LIGHT_CHANGED", {level: "DARK"})
  → TriggerMap → ISM 状态变更 → 发布 ssm/agents/esp32_desk_light/event
  → 云端 ESP32 桌面智能体收到事件 → 推理
  → 发布 ssm/task/esp32_desk_led/{task_id}（SET_COLOR 暖白）
  → ESP32 执行 → 发布 ssm/result/... {ok}
```

**手机自然语言控制：**
```
用户说"把灯调暗一点"
  → 手机发布 ssm/intent/{session_id}
  → 编排器 Planner 生成任务 → Dispatcher 下发（ESP32 走 MQTT / Go2 走 HTTP）
  → 设备执行 → Evaluator 确认 → 手机收到 ssm/feedback/{session_id}
```
