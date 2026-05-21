# SSM 项目 — Codex 工作指引

## 项目概述

**一句话**：SSM（Smart System Mesh）是一个以 MQTT 为消息总线的分布式多智能体 IoT 系统，让 ESP32 边缘设备、云端 AI 编排器和手机 PWA 通过标准化 topic 协议自主协作，无需任何中心控制器。

**目标用户**：探索边缘-云端-移动端三层分布式智能体架构的开发者；核心体验是"新接一个传感器，整个系统自动感知并纳入决策"。

**核心业务逻辑**：
1. ESP32 启动时探测引脚 → 向 broker 发布已接设备的 manifest（retained）
2. 手机 PWA 订阅 `ssm/agents/#`，自动发现所有在线设备，按 GPS 距离排序
3. 用户语音/文字 → `/api/nlu` 解析意图 → 编排器 Planner→Dispatcher→Evaluator→Responder 执行并反馈
4. 传感器事件也触发编排器自动决策（如变暗自动开灯）
5. 云端离线时 ESP32 本地规则接管（兜底层）

---

## 技术栈

| 层 | 技术 | 版本 |
|----|------|------|
| 边缘端 | MicroPython on ESP32 | MicroPython ≥1.23，ESP32 DevKit |
| 边缘库 | `umqtt.robust`、`neopixel`、`machine`、`ujson` | 内置于 MicroPython |
| 消息代理 | Mosquitto | 运行于云服务器 `47.116.137.202`，TCP 1883 / WS 9001 |
| 云端 AI | LangGraph + LangChain + ChatOpenAI（兼容接口） | langgraph≥0.2、langchain≥0.3 |
| LLM | NVIDIA OpenAI-compatible API，`deepseek-ai/deepseek-v4-pro` | 见 `server/.env: MODEL_LIST` |
| 云端 API | FastAPI + uvicorn | fastapi≥0.136、uvicorn[standard]≥0.46 |
| 手机 PWA | React 18（无构建，Babel standalone） + MQTT.js | React 18.3.1、mqtt.js 5.3.4 |
| Python 环境 | uv 管理，Python 3.13 | `pyproject.toml` + `uv.lock` |

---

## 项目结构地图

```
ssm/
├── agents/
│   ├── esp32/               ← MicroPython 边缘智能体
│   │   ├── config.py        ← ★ 唯一配置入口：引脚、MQTT、UNIT_CONFIGS 注册表
│   │   ├── probe.py         ← 通用引脚探测引擎（读 UNIT_CONFIGS，零硬编码）
│   │   ├── agent_manifest.py← 通用 manifest 发布（读 UNIT_CONFIGS + PRESENCE）
│   │   ├── bsm.py           ← 硬件驱动层（GPIO/ADC/PWM），只抛事件
│   │   ├── ism.py           ← 状态机契约层，只做状态转换
│   │   ├── trigger_map.py   ← ★ 唯一耦合点：BSM↔ISM↔MQTT 接线
│   │   ├── local_rules.py   ← 云端离线兜底规则
│   │   ├── mqtt_client.py   ← MQTT 封装，自动重连 + LWT
│   │   ├── boot.py          ← WiFi 连接（先于 main.py 执行）
│   │   └── main.py          ← 主循环入口
│   └── phone/               ← PWA 控制界面
│       ├── src/app.jsx      ← ★ 主 React 应用（雷达、设备、规则、对话）
│       ├── src/MqttBus.js   ← MQTT 连接与发布/订阅封装
│       ├── src/AgentRegistry.js ← 设备注册表（监听 manifest/status/location）
│       └── src/ISMTracker.js    ← 设备状态跟踪（监听 state/event/report）
├── server/
│   ├── .env                 ← LLM API Key、MQTT 地址、MODEL_LIST
│   ├── api/main.py          ← FastAPI：/api/nlu、/api/rules
│   └── orchestrator/
│       ├── graph.py         ← LangGraph 图：V1 ReAct + V2 Planner→Dispatcher→Evaluator→Responder
│       ├── tools.py         ← @tool 函数：能力查询、传感器快照、指令发布
│       ├── shared_state.py  ← 线程安全能力注册表 + 任务结果缓存
│       └── main.py          ← MQTT 事件循环，路由 V1/V2
├── broker/mosquitto.conf    ← Broker 配置
├── Makefile                 ← 统一服务管理
└── docs/ARCHITECTURE.md     ← 通信架构单一真实来源
```

**当前接线**（以 `config.py` 为准）：

| 硬件 | GPIO | 状态 |
|------|------|------|
| WS2812 灯环 | GPIO4 | 已接 |
| 蜂鸣器（无源） | GPIO5 | 未接（`BUZZER_ENABLED=False`） |
| 光线传感器 | GPIO34 | 已接（ADC 模拟） |
| 红外传感器 | GPIO19 | 未接 |
| 声音传感器 | GPIO15 | 已接 |

---

## 编码规范

### 命名规则

| 对象 | 规范 | 示例 |
|------|------|------|
| ESP32 agent ID | `{device_id}_{unit}` | `esp32_desk_led` |
| MQTT topic | `ssm/agents/{unit_id}/{type}` | `ssm/agents/esp32_desk_led/state` |
| Python 变量/函数 | `snake_case` | `probe_digital`, `ism_light` |
| MicroPython 常量 | `UPPER_SNAKE` | `BUZZER_PIN`, `UNIT_CONFIGS` |
| LangGraph 节点 | `{role}_node` | `planner_node`, `evaluator_node` |

### MicroPython 特定注意事项

- **不用列表推导式存大集合**：内存有限（~300KB 可用），优先用生成器或原地修改
- **用 `ujson` 代替 `json`**：ESP32 内置，速度更快，API 相同
- **不用 `asyncio`**：当前架构用同步主循环（`while True: bsm.tick(); mqtt.check_msg()`），引入 asyncio 会破坏现有 tick 节奏
- **`time.ticks_ms()` + `time.ticks_diff()`**：ESP32 的 `ticks_ms` 会溢出，必须用 `ticks_diff` 做差值，不能直接相减
- **引脚释放**：probe.py 探测后必须 `Pin(n, Pin.IN)` 释放引脚，否则 BSM 初始化时引脚模式冲突
- **每条 MQTT 消息带 `ts` 和 `agent_id`**：`ts = time.time()`（Unix 秒），非 `ticks_ms`

### 云端 / PWA

- 云端服务始终用 `uv run python`，不手动激活 venv
- PWA 无构建步骤，直接编辑 `.jsx`，浏览器刷新即生效
- PWA 内所有 MQTT 操作通过 `MqttBus` 单例，不直接调用 `mqtt.js` API

---

## 架构决策

### 1. 分布式多智能体 + MQTT 总线（核心）

**决策**：所有智能体（ESP32、编排器、PWA）只通过 MQTT topic 通信，无直接 RPC 调用。

**原因**：天然解耦——新加一个 ESP32 只需发布 manifest，其余节点自动感知；编排器宕机不影响 ESP32 本地规则；各层可独立替换。

**避免**：ESP32 直接调用 HTTP API、编排器直接操作 GPIO、PWA 轮询 REST 状态。

### 2. ESP32 三层解耦（BSM / ISM / TriggerMap）

**决策**：BSM 驱动硬件只抛事件；ISM 只做状态转换契约；TriggerMap 是两者与 MQTT 的唯一接线点。

**原因**：新增一个行为（如"声音触发闪烁"）只改 TriggerMap 一处，BSM 和 ISM 不需要知道对方。可以独立测试每一层。

**避免**：在 BSM 里发 MQTT 消息；在 ISM 里读 GPIO；在 TriggerMap 以外做 ISM↔BSM 的直接调用。

### 3. UNIT_CONFIGS 作为设备注册表（普适性）

**决策**：每个 unit 的引脚、探测策略、manifest 元数据全部声明在 `config.py` 的 `UNIT_CONFIGS` 字典里，`probe.py` 和 `agent_manifest.py` 是纯通用引擎。

**原因**：新增传感器只改 `config.py` 一行，其余文件零修改；保证系统天然支持任意数量的同类设备。

**避免**：在 `probe.py`、`agent_manifest.py`、`bsm.py` 里硬编码具体 unit 名称或引脚号。

### 4. LangGraph Planner→Dispatcher→Evaluator→Responder（V2 编排）

**决策**：用有向图而不是单一 LLM 调用处理用户意图；每个节点职责单一。

**原因**：Planner 只生成任务列表；Dispatcher 只发 MQTT；Evaluator 只校验结果；Responder 只组织回复。每个节点可以独立更换模型或逻辑。LLM fallback 链（多模型按顺序重试）也在图内透明处理。

**避免**：在一个节点里同时做规划+执行+评估；在 tools.py 里写业务逻辑（tools 只做 MQTT 操作和状态查询）。

### 5. PWA 无"订阅"UI 状态

**决策**：PWA 在 MQTT 层订阅 `ssm/agents/#` 通配符，接收所有消息。设备发现和显示是自动的，不存在用户手动"订阅设备"的 UI 步骤。

**原因**：原有"长按订阅"逻辑和 MQTT 订阅无关，是误导性的 UI 概念；去掉后设备接入体验从"发现→订阅→控制"变为"发现→控制"。

**避免**：重新引入 `subscribed` state、`toggleSub` 或 `localStorage` 的订阅持久化。

---

## 验证方法

```bash
# 查看所有服务进程
make ps

# 查看后台日志
make logs

# ESP32 重启后串口应看到：
# [Probe] {'esp32_desk_light': True, 'esp32_desk_ir': False, 'esp32_desk_sound': True, ...}
# [Manifest] Published N manifests for esp32_desk

# PWA 验证：打开 DevTools Console 观察 MQTT 消息收发
make ngrok-url
```

**改动验证节奏**：每次只改一处，5 分钟内能在 PWA 或串口看到结果。不能快速验证的改动说明粒度过大。

---

## QA 边界

**可以做：**
- 在 `UNIT_CONFIGS` 新增设备条目
- 在 `TriggerMap` 新增事件→动作路由
- 在 `graph.py` 调整 LangGraph 节点逻辑
- 修改 PWA UI 样式和交互（app.jsx）

**不能做：**
- 在 BSM 里调用 MQTT 或 ISM
- 在 ISM 里读 GPIO
- 在 `probe.py` / `agent_manifest.py` 里硬编码 unit 名称
- 在 PWA 里重新引入 subscribed/toggleSub 状态
- 为"将来可能有用"的功能写代码（MVP 原则）
