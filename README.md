# SSM — 智能系统网格

基于 MQTT 消息总线的多智能体 IoT 系统，连接 ESP32 边缘设备、云端 AI 决策智能体和手机 PWA 控制界面。

## 系统架构

```
┌─────────────────────────────────────────────────────────────┐
│                       MQTT 消息代理                          │
│                   Mosquitto :1883/:9001                      │
└──────────────────────┬──────────────────┬───────────────────┘
                       │                  │
          ┌────────────▼───────┐   ┌──────▼────────────┐
          │   ESP32（边缘端）   │   │  云端决策智能体    │
          │   MicroPython      │   │  LangGraph + LLM   │
          │                    │   │                    │
          │  BSM ← TriggerMap  │   │  V1 决策智能体     │
          │  ISM ← TriggerMap  │   │  V2 编排器         │
          │  本地规则           │   │                    │
          └────────┬───────────┘   └──────────────────┬─┘
                   │                                   │
          ┌────────▼───────────────────────────────────▼─────┐
          │                手机 PWA（监控 + 对话）             │
          │         WebSocket MQTT，自然语言控制               │
          └───────────────────────────────────────────────────┘
```

**控制优先级**：手机指令 > 云端决策智能体 > ESP32 本地规则（离线兜底）

## 硬件引脚

| 硬件 | GPIO | 模式 |
|------|------|------|
| WS2812 灯环（数据线） | GPIO4 | 单总线 |
| 蜂鸣器（无源） | GPIO5 | PWM |
| 光线传感器 | GPIO34 | 模拟（ADC1） |
| 红外传感器 | GPIO19 | 数字，低电平有效 |
| 声音传感器 | GPIO15 | 数字，上升沿触发 |

传感器是否在线由 `probe.py` 在启动时自动探测（拉电阻技巧），无需手动配置开关。

## 各组件说明

### ESP32（`agents/esp32/`）

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

### 云端服务（`server/`）

#### Chat API（`server/api/main.py`）

FastAPI 服务，端口 8082。

| 接口 | 说明 |
|------|------|
| `POST /api/nlu` | 意图解析：返回结构化需求 + session_id |
| `POST /api/rules` | 规则管理：增删改查自动化规则 |

#### 决策编排器（`server/orchestrator/`）

Python + LangGraph，订阅所有 MQTT 事件，驱动两条决策路径。

| 文件 | 职责 |
|------|------|
| `main.py` | MQTT 事件循环，路由到 V1/V2 图 |
| `graph.py` | LangGraph：V1 ReAct 决策图 + V2 编排器 |
| `tools.py` | LangChain @tool：能力查询、传感器快照、指令发布等 |
| `shared_state.py` | 线程安全状态存储（能力注册表、任务结果） |

**环境变量**：统一存放在 `server/.env`（LLM API Key、MQTT 地址、模型名等）。

### 手机 PWA（`agents/phone/`）

无构建步骤的 React 应用，通过 WebSocket 连接 MQTT 代理（端口 9001）。自动订阅 `ssm/agents/#`，实时接收所有设备状态。

功能：真实 GPS 雷达图、传感器实时数据、执行器控制、自然语言对话、自动化规则管理。

## MQTT 消息协议

每个 agent unit 发布以下 topic：

| 类型 | Topic | 保留 | 说明 |
|------|-------|------|------|
| `manifest` | `ssm/agents/{id}/manifest` | 是 | 设备能力声明，启动时发布 |
| `state` | `ssm/agents/{id}/state` | 是 | 当前 ISM 状态，变更时发布 |
| `event` | `ssm/agents/{id}/event` | 否 | 传感器事件或执行器触发 |
| `report` | `ssm/agents/{id}/report` | 否 | 传感器观测值或执行器反馈 |
| `location` | `ssm/agents/{id}/location` | 是 | 设备地理坐标（GCJ-02） |

**控制 topic**：
- `ssm/agents/{id}/command` —— 执行器直接指令
- `ssm/intent/{session_id}` —— 手机意图（NLU 解析后发布）
- `ssm/task/{device_id}/{task_id}` —— 编排任务
- `ssm/feedback/{session_id}` —— 渐进式执行反馈

所有 payload 均为 JSON，每条消息带 `agent_id` 和 `ts`（Unix 时间戳）。

## 快速上手

### 0. Python 环境

```bash
uv sync          # 安装所有依赖
uv add <包名>    # 添加新依赖
```

始终在项目根目录 `/root/ssm` 执行 uv 命令；运行脚本用 `uv run python`，无需激活虚拟环境。

### 1. 启动云端服务

```bash
make broker        # 启动 MQTT Broker
make api-bg        # 后台启动 Chat API（端口 8082）
make orchestrator  # 前台启动决策智能体
make pwa-bg        # 后台启动 PWA 文件服务（端口 8081）
make ngrok-bg      # 后台启动 ngrok HTTPS 隧道
make ngrok-url     # 查询当前公网地址
```

nginx 反向代理开机自启，配置位于 `/etc/nginx/conf.d/ssm.conf`。

### 2. 配置并上传 ESP32

编辑 `agents/esp32/config.py`，填入 WiFi 账号和 Broker 地址，通过 [mpremote](https://docs.micropython.org/en/latest/reference/mpremote.html) 上传：

```bash
cd agents/esp32
mpremote connect <端口> cp boot.py config.py probe.py ism.py bsm.py \
  mqtt_client.py trigger_map.py local_rules.py agent_manifest.py main.py :
mpremote connect <端口> reset
```

上传后串口应输出 `[Probe] {...}` 显示各传感器探测结果。

### 3. 打开手机 PWA

浏览器访问 `make ngrok-url` 输出的公网地址，允许位置权限后即可看到附近设备雷达图。

## 网络拓扑

```
手机浏览器（HTTPS）
    │
    ▼
ngrok 公网域名（https://xxx.ngrok-free.dev）  ← 提供 HTTPS/WSS
    │
    ▼ HTTP（端口 8080）
nginx
    ├── /        → PWA 静态文件（端口 8081）
    └── /mqtt    → Mosquitto WebSocket（端口 9001）

ESP32
    └── TCP 1883 → Mosquitto（直连，无需 TLS）
```

PWA 内 MQTT 地址自动切换：HTTPS 访问时用 `wss://{ngrok域名}/mqtt`，HTTP 访问时直连 `ws://47.116.137.202:9001`。

## 数据流示例

**传感器自动响应：**
```
光线变暗
  → BSM 检测 GPIO34 ADC 值下降 → event_cb("LIGHT_CHANGED", {level: "DARK"})
  → TriggerMap → ISM_LIGHT 状态变更 → 发布 ssm/agents/esp32_desk_light/event
  → 云端决策智能体收到事件 → 查询可用设备能力
  → 发布 ssm/task/esp32_desk_led/{task_id}（SET_COLOR 暖白）
  → ESP32 执行 → 发布 ssm/result/... {ok}
```

**手机自然语言控制：**
```
用户说"把灯调暗一点"
  → /api/nlu 解析意图 → 返回 session_id + requirements
  → 手机发布 ssm/intent/{session_id}
  → 编排器 Planner 生成任务 → Dispatcher 下发
  → ESP32 执行 → Evaluator 确认 → 手机收到 ssm/feedback/{session_id}
```
