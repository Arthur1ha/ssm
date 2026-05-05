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
| 光线传感器 | GPIO18 | 数字（二值） |
| 红外传感器 | GPIO19 | 数字，低电平有效 |
| 声音传感器 | GPIO15 | 数字，上升沿触发 |

## 各组件说明

### ESP32（`agents/esp32/`）

运行在边缘设备上的 MicroPython 代码。

| 文件 | 职责 |
|------|------|
| `boot.py` | 启动时连接 WiFi |
| `config.py` | 引脚、MQTT 代理地址、时序常量 |
| `bsm.py` | 行为状态机——GPIO/PWM/ADC 驱动，事件回调 |
| `ism.py` | 接口状态机——仅处理状态转换，不涉及硬件 |
| `trigger_map.py` | BSM ↔ ISM ↔ MQTT 接线（唯一耦合点） |
| `local_rules.py` | 云端智能体离线时的本地自动规则 |
| `agent_manifest.py` | 启动时发布 5 个 unit 的 manifest |
| `mqtt_client.py` | MQTT 封装，支持自动重连和 LWT |
| `main.py` | 主循环入口 |

**ISM 单元**：`esp32_desk_light`、`esp32_desk_ir`、`esp32_desk_sound`、`esp32_desk_led`、`esp32_desk_buz`

**本地规则**（`ssm/decision/active = "false"` 时启用）：
- 光线 DARK/DIM → 暖白光 LED（R=255, G=160, B=60）
- 光线 BRIGHT → 关灯
- 检测到声音 → 白色闪烁 ×2（始终生效）

### 云端服务（`server/`）

#### Chat API（`server/api/main.py`）

FastAPI 服务，端口 8082。

| 接口 | 说明 |
|------|------|
| `POST /api/chat` | V1 对话：LLM 直接生成 MQTT 指令 |
| `POST /api/nlu` | V2 意图解析：返回结构化需求 + session_id |

#### 决策编排器（`server/orchestrator/`）

Python + LangGraph，订阅所有 MQTT 事件，驱动两条决策路径。

| 文件 | 职责 |
|------|------|
| `main.py` | MQTT 事件循环，路由到 V1/V2 图 |
| `graph.py` | LangGraph：V1 ReAct 决策图 + V2 编排器（Planner→Dispatcher→Evaluator→Responder） |
| `tools.py` | LangChain @tool：`get_capabilities`、`get_sensor_snapshot`、指令发布等 |
| `shared_state.py` | 线程安全状态存储（能力注册表、任务结果） |

**环境变量**：统一存放在 `server/.env`（LLM API Key、MQTT 地址、模型名等）。

### 手机 PWA（`agents/phone/`）

无构建步骤的 React 应用，通过 WebSocket 连接 MQTT 代理（端口 9001）。

功能：设备发现与距离排序、实时 ISM 状态跟踪、自然语言对话控制。

## MQTT 消息协议

每个 agent unit 精确发布以下 4 类 topic：

| 类型 | Topic | 保留 | 说明 |
|------|-------|------|------|
| `manifest` | `ssm/agents/{id}/manifest` | 是 | 设备能力声明，启动时发布 |
| `state` | `ssm/agents/{id}/state` | 是 | 当前 ISM 状态，变更时发布 |
| `event` | `ssm/agents/{id}/event` | 否 | 传感器事件或执行器触发 |
| `report` | `ssm/agents/{id}/report` | 否 | 传感器观测值或执行器反馈 |

**V1 控制 topic**：
- `ssm/agents/{id}/command` —— LED/蜂鸣器直接指令
- `ssm/decision/active` —— `"true"` = 云端接管，ESP32 本地规则停用
- `ssm/decision/evaluation` —— 执行效果评估结论

**V2 编排 topic**：
- `ssm/intent/{session_id}` —— 手机意图（NLU 解析后发布）
- `ssm/task/{device_id}/{task_id}` —— 编排任务（action + params）
- `ssm/result/{device_id}/{task_id}` —— 任务执行结果（ok/blocked/timeout）
- `ssm/feedback/{session_id}` —— 渐进式反馈（planning/executing/done 等）

所有 payload 均为 JSON，每条消息带 `agent_id` 和 `ts`（Unix 时间戳）字段。

## 快速上手

### 0. Python 环境

项目使用 [uv](https://github.com/astral-sh/uv) 管理（`pyproject.toml` + `uv.lock`）。

```bash
# 安装所有依赖
uv sync

# 添加新依赖
uv add <包名>
```

运行脚本无需激活虚拟环境，直接 `uv run python` 即可。

### 1. 启动云端服务

在 `/root/ssm` 目录下：

```bash
make broker        # 启动 MQTT Broker
make api-bg        # 后台启动 Chat API（端口 8082）
make orchestrator  # 前台启动决策智能体
make pwa-bg        # 后台启动 PWA 文件服务（端口 8081）
make ngrok-bg      # 后台启动 ngrok HTTPS 隧道
make ngrok-url     # 查询当前公网地址
```

### 2. 配置 ESP32

编辑 `agents/esp32/config.py`，填入 WiFi 账号、Broker 地址和 MQTT 认证信息。

通过 [mpremote](https://docs.micropython.org/en/latest/reference/mpremote.html) 上传文件：

```bash
cd agents/esp32
mpremote connect COM7 cp boot.py config.py ism.py bsm.py mqtt_client.py trigger_map.py local_rules.py agent_manifest.py main.py :
mpremote connect COM7 reset
```

### 3. 打开手机 PWA

浏览器访问 `make ngrok-url` 输出的公网地址即可。

## 数据流示例

**V1——传感器自动响应：**
```
光线传感器变暗
  → BSM 检测 GPIO18 低电平 → 触发 event_cb("LIGHT_CHANGED", {level: "DARK"})
  → TriggerMap 转换 ISM_LIGHT → 发布 ssm/agents/esp32_desk_light/event
  → 云端决策智能体收到事件 → 调用 get_capabilities 查询可用设备
  → 调用 publish_led_command(SET_COLOR, r=255, g=160, b=60)
  → ESP32 收到 ssm/agents/esp32_desk_led/command
  → TriggerMap 转换 ISM_LED → BSM 设置 RGB PWM
  → 发布 ssm/agents/esp32_desk_led/report {result: "ok"}
  → 评估智能体比对决策与反馈 → 发布评估结论
```

**V2——手机自然语言控制：**
```
用户说"帮我把灯调暗一点"
  → /api/nlu 解析意图 → 返回 session_id + requirements
  → 手机发布 ssm/intent/{session_id}
  → 编排器 Planner 查询能力注册表 → 生成任务列表
  → Dispatcher 发布 ssm/task/esp32_desk_led/{task_id}
  → ESP32 执行 → 发布 ssm/result/esp32_desk_led/{task_id}
  → Evaluator 确认结果 → Responder 发布 ssm/feedback/{session_id}
  → 手机收到渐进式反馈，更新 UI
```

## 设计原则

- **ISM** 只知道状态和合法转换——不涉及硬件，不涉及 MQTT
- **BSM** 驱动硬件并向上触发事件——不知道 MQTT，不知道 ISM
- **TriggerMap** 是唯一连接三层的文件
- 每个新增行为对应一处文件改动，5 分钟内可验证
