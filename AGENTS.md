# SSM 项目 — Codex 工作指引

## 项目概述

**一句话**：SSM（Smart System Mesh）是一个分布式多智能体 IoT 系统——以 MQTT 为统一**控制总线**（发现、在线状态、能力声明 card），**数据面按设备本性选传输**（ESP32 走 MQTT、Go2 走 HTTP/SSE），让 ESP32 边缘设备、云端管家/灯/Go2 Graph 和手机 PWA 协作；边缘硬件 tick 独立于云端编排器运行。

**目标用户**：探索边缘-云端-移动端三层分布式智能体架构的开发者；核心体验是"新接一个传感器，整个系统自动感知并纳入决策"。

**核心业务逻辑**：

1. ESP32 启动时探测引脚 → 为已接智能体发布 retained manifest；传感器仅发布独立的 state/event，不产生设备卡
2. 云端 CardRegistry 统一处理 retained manifest/card/status，API 汇集 state/event；管理员把发现的设备加入固定空间 `main` 后，PWA 经 `/api/devices` 与 `/api/events`（SSE）自动展示和同步，无手动订阅步骤
3. 用户语音/文字 → 已认证 PWA 请求 `/api/intents` → API 生成可信上下文并发布签名 MQTT `ssm/intent/{session_id}` → 管家 Graph 逐轮选择一个工具、等待真实结果、继续规划，最后生成人格回复；设备聊天/按钮走 `/api/commands/{unit_id}`
4. `reactive` 模式下，传感器事件也可触发云端 ESP32 智能体自主决策；默认 `manual` 不自发控灯
5. ESP32 独立执行硬件 tick、任务去重与结果回报；当前固件没有离线自动控灯规则，云端离线不触发新的自主决策
6. 智能体通过 MQTT retained manifest/card 发布能力，`/api/devices/{unit_id}/agent` 返回空间内已发现设备的静态 A2A Agent Card；HTTP 是读取投影，发现真相仍在 MQTT
7. 统一教导走 `/api/training` 草案→管理员确认→执行器加载，按教导 ID/版本执行单动作、多动作或场景参数；保存、加载、物理完成分别判定
8. 空间活动通过用户请求启动，灯进入 `reactive`、Go2 进入 `free_explore`；同伴自主消息、观察、动作和结果进入活动时间线。新用户介入结束活动并撤销其模式归属；重启恢复不重发旧物理动作

**事实来源**：启动命令见 [Makefile](Makefile)，依赖见 [pyproject.toml](pyproject.toml) 与 `uv.lock`，配置见 [cloud/.env.example](cloud/.env.example) 和 [edge/config.py](edge/config.py)，协议见 [protocol/topics.md](protocol/topics.md)、[protocol/agent-card.md](protocol/agent-card.md)。README 面向使用与部署，本文件面向后续代码修改；更新行为时同步维护相关文档。`docs/` 的计划、历史证据和 `.superpowers/` 基线副本不是当前运行契约，不把历史验证结果写成当前已通过。

---

## 技术栈

| 层 | 技术 | 版本 |
|----|------|------|
| 边缘端 | MicroPython on ESP32 | 使用兼容固件，当前目标为 ESP32 DevKit |
| 边缘库 | `umqtt.simple`、`neopixel`、`machine`、`ujson` | 需确认固件提供或另行安装 `umqtt.simple` |
| 消息代理 | Mosquitto + paho-mqtt | 模板仅 TCP 1883，无 WS 9001；云端 paho `>=1.6.1,<2.0` |
| 云端 AI | LangChain 工具调用 + LangGraph 逐步任务图、文件检查点 | langchain≥1、langgraph≥1 |
| LLM | 兼容 OpenAI 的服务，统一由 `agents/common/models.py` 构造 | 管家用 `MODEL_LIST`；灯/Go2 用 `DEVICE_TEXT_MODEL`；视觉用 `GO2_VISION_MODEL` |
| Go2 机器狗 | `unitree-webrtc-connect` ≥2.1.2 + OpenCV HOG/Haar + 视觉语言模型、RGB/LiDAR | 数据面 HTTP/SSE/WebRTC，控制面 card/status/state 仍走 MQTT |
| 人员库 | 腾讯云 IAI 3.0 + 空间人员元数据 | 手机照片登记，见 `cloud/space/people/` |
| 云端 API | FastAPI + uvicorn | fastapi≥0.136、uvicorn[standard]≥0.46 |
| 手机 PWA | React 18（无构建，Babel standalone）+ 同源 HTTP/SSE | React 18.3.1 |
| Python 环境 / 验证 | uv 管理，Python ≥3.13；pytest | `pyproject.toml` + `uv.lock`；前端另有 Node 回归脚本 |

---

## 项目结构地图

```
ssm/
├── edge/                ← MicroPython 边缘智能体（跑在 ESP32 上）
│   ├── config.py        ← ★ 引脚、时序、UNIT_CONFIGS 注册表；密钥从 local_config.py 读取
│   ├── local_config.example.py ← WiFi/MQTT 配置模板，实际 local_config.py 不提交
│   ├── probe.py         ← 通用引脚探测引擎（读 UNIT_CONFIGS，零硬编码）
│   ├── agent_manifest.py← 智能体 manifest 发布及旧传感器 manifest 清理（读 UNIT_CONFIGS + PRESENCE）
│   ├── bsm.py           ← 硬件驱动层（GPIO/ADC/PWM），只抛事件
│   ├── ism.py           ← 状态机契约层，只做状态转换
│   ├── trigger_map.py   ← ★ 唯一耦合点：BSM↔ISM↔MQTT 接线
│   ├── lamp_contract.py← 灯动作、参数与持续闪烁默认值的固件契约
│   ├── clock.py         ← NTP 校时与 Unix 时间换算
│   ├── mqtt_client.py   ← MQTT 封装，自动重连 + LWT
│   ├── boot.py          ← WiFi 连接（先于 main.py 执行）
│   └── main.py          ← 主循环入口
├── cloud/               ← 云端服务（跑在服务器上）
│   ├── .env.example     ← 配置模板；实际 .env 存放模型/MQTT/内部服务/会话凭据，不提交
│   ├── api/             ← FastAPI 聚合入口（端口 8082），装配下面所有 router
│   │   ├── main.py      ← app 装配 + ESP32 MQTT 桥（订阅 manifest/state/event/result）
│   │   ├── auth.py / sessions.py ← Cookie/CSRF、固定 main 空间管理与邀请
│   │   ├── intents.py   ← 签名意图、统一 commands、会话 SSE、任务查询与清理
│   │   ├── training.py  ← `/api/training` 教导、草案与确认
│   │   ├── activity.py  ← 空间活动生命周期与时间线
│   │   ├── personality.py / people.py ← 人格配置与空间人员库
│   │   └── devices.py   ← `/api/devices` + `/api/devices/{unit_id}/agent`（★ A2A Agent Card，数据取自 cards/）
│   ├── cards/           ← ★ A2A 设备卡注册表：registry.py（订阅 card/manifest/status，全局单一真相）、builder.py（manifest→AgentCard）、schema.py
│   ├── agents/          ← 三角色 Agent Graph 与共享文件检查点、动作账本、回复投影
│   │   ├── common/      ← 运行图、持久化、模型、事件与回复契约；training/ 统一教导
│   │   ├── steward/     ← 管家 Graph、人格、工具声明与公开 skill
│   │   ├── lamp/        ← 灯 Graph、人格、记忆与自主工作流
│   │   └── go2/         ← Go2 Graph、人格、记忆与自主工作流
│   ├── orchestrator/    ← 云端编排器（独立进程，MQTT 事件循环 + 逐步任务循环）
│   │   ├── main.py      ← MQTT 事件循环：`ssm/intent/+` → 管家 Graph
│   │   ├── tools.py     ← MQTT/HTTP 派发与反馈辅助
│   │   ├── shared_state.py ← 线程安全 MQTT 传感器/执行器快照与可信会话元数据
│   │   ├── trusted_context.py ← 签名意图、同伴请求与会话清理校验
│   │   └── card.py      ← 管家公开能力卡
│   ├── esp32/           ← 灯的云端服务与设备执行适配
│   │   ├── service.py   ← 灯实例与自主模式状态、Graph 入口
│   │   ├── router.py    ← 自主模式、对话、内部语义任务与物理回执接口
│   │   ├── state.py     ← ESP32 设备/任务状态
│   │   ├── perception.py / presentation.py ← 传感器事实整理与控制状态投影
│   │   ├── tool_registry.py / tool_executor.py ← 动作契约、执行校验与回执等待
│   │   └── tasks.py / physical_tasks.py ← 语义任务与物理派发的持久去重账本
│   ├── go2/             ← Go2 设备服务（数据面 HTTP；控制面 card/status 走 MQTT）
│   │   ├── router.py    ← `/api/go2/*` 全部端点（连接/运动/导航/视觉/对话）
│   │   ├── service.py / state.py / actions.py ← 模式装配、分层状态与动作契约
│   │   ├── tasks.py / control.py ← 任务生命周期与运动租约
│   │   ├── mode/、perception/、tools/ ← 自主调度、视觉与执行适配
│   │   ├── navigation/  ← 路径规划、地图、目标接近与导航执行
│   │   └── connection/  ← webrtc / video / sensors / fsm
│   ├── personality/     ← 三角色共用的人格存储、版本与名称管理
│   ├── peers/           ← 同伴发现、签名通信、技能请求、权限与互动游戏
│   │   └── games/      ← 按游戏分文件的规则、玩法、策略与执行适配；统一注册表汇总
│   ├── space/           ← 空间成员、可信执行上下文、用户约束、活动与人员库
│   │   ├── activity.py / decisions.py ← 活动持久化、历史事实与决策证据校验
│   │   └── people/     ← 人员元数据与腾讯云提供方
│   ├── observability/   ← 日志配置、任务关联事件与脱敏
│   └── storage/         ← 原子 JSON 文件读写与锁
├── app/                 ← 用户端 PWA（跑在手机/浏览器）
│   ├── serve.py         ← 本地 127.0.0.1:8081，按文件时间给 JS/CSS URL 加版本
│   ├── index.html / manifest.json / sw.js / styles/app.css
│   └── src/
│       ├── app.jsx      ← ★ 主 React 应用（单屏聊天中心 + hash 子页、聊天状态）
│       ├── ApiClient.js / CommandBus.js ← 会话 API/SSE 与设备命令入口
│       ├── hooks/       ← useSendIntent、useSsmCore、useTraining、useSpaceActivity、useActivityTimeline
│       ├── pages/       ← FsmDevicePage、DevicesPage 与状态机图组件
│       ├── components/  ← SessionGate、ActivityFeed/ActivityDetails、ChatPanel、TrainingDrawer、SpacePeopleDrawer 等
│       └── utils/       ← agentMeta、stateMachine、stateSnapshot、activityView、color、二维码工具
├── protocol/            ← MQTT 协议契约（单一真实来源）
│   ├── topics.md        ← MQTT 消息、签名封套、状态与任务语义
│   ├── agent-card.md    ← 静态技能、regions 拓扑、呈现与动作门禁
│   └── identifiers.md   ← device_id / unit_id / session_id 标识约定
├── infra/               ← 运维与部署配置
│   └── broker/
│       ├── mosquitto.conf.tmpl ← Broker 配置模板（envsubst 生成）
│       ├── acl.example  ← 专用账号权限模板，需部署时显式启用
│       └── passwd       ← 本机 MQTT 认证文件，不随仓库分发
├── scripts/             ← trace_task、tmux、前端回归和 Go2 诊断/回放工具
├── Makefile             ← 统一服务管理
├── OBSERVABILITY.md     ← 日志、会话追踪与排查说明
├── pyproject.toml       ← Python 依赖（uv 管理）
└── docs/                ← architecture/、superpowers/（plans + specs）、research/；部分本地资料被忽略
```

**云端有三个 LLM 智能体**：管家、灯与 Go2 的模型决策分别位于 `cloud/agents/{steward,lamp,go2}/`；`orchestrator/main.py`、`esp32/service.py` 与 `go2/router.py` 接入各自 Graph。编排器经 MQTT 与 ESP32 通信、经 HTTP 调用 Go2。灯的定性照明目标也可通过内部 HTTP 语义任务交给灯 Graph，物理执行仍走 MQTT。`go2/mode/` 负责自主事件调度，决策 Graph 位于 `agents/go2/workflows/`。

**目录边界**：`agents/` 定义角色决策；`esp32/`、`go2/`、`orchestrator/` 保留服务入口和设备适配；`personality/`、`peers/`、`space/`、`observability/` 提供共享能力。设备发现与在线状态以 MQTT retained card/manifest/status 为准，空间成员配置在 `cloud/space/space.json`；不存在文件型设备能力注册表或旧 `/api/rules` 入口。

**运行数据**：共享检查点、运行索引、动作账本和教导默认位于 `cloud/agents/data/{checkpoints,run_index,effects,training}/`；灯与 Go2 的任务/记忆位于各自 `data/`，活动与人员在 `cloud/space/data/`。验证使用临时目录或 fixture，不改写现有运行资产，不读取或输出密钥。`cloud/agents/data/training/` 中已有用户配置也按运行资产处理。

**当前接线**（以 `config.py` 为准）：

| 硬件 | GPIO | 状态 |
|------|------|------|
| WS2812 灯环 | GPIO4 | 已接 |
| 光线传感器 | GPIO34 | 已接（ADC 模拟） |
| 红外传感器 | GPIO19 | 未接（`UNIT_CONFIGS` 有条目，探测缺席不发 state/event） |
| 声音传感器 | GPIO15 | 已接，开漏输出，使用 `flag` 显式启用 |

> 接线状态为项目记录，实际存在性看启动 `PRESENCE`。蜂鸣器在 `config.py` 中无条目。光线和红外使用 ADC/数字探测，声音与灯使用 `flag`，不能把所有单元描述成自动电气探测。

---

## 编码规范

### 命名规则

| 对象 | 规范 | 示例 |
|------|------|------|
| ESP32 agent ID | `{device_id}_{unit}` | `esp32_desk_led` |
| MQTT topic | `ssm/agents/{unit_id}/{type}` | `ssm/agents/esp32_desk_led/state` |
| Python 变量/函数 | `snake_case` | `probe_digital`, `ism_light` |
| MicroPython 常量 | `UPPER_SNAKE` | `WS2812_PIN`, `UNIT_CONFIGS` |
| 智能体任务循环 | `run_{agent}_loop` | `run_go2_loop`, `run_lamp_loop` |

### MicroPython 特定注意事项

- **不用列表推导式存大集合**：内存有限（~300KB 可用），优先用生成器或原地修改
- **用 `ujson` 代替 `json`**：ESP32 内置，速度更快，API 相同
- **不用 `asyncio`**：当前架构用同步主循环（`while True: bsm.tick(); mqtt.check_msg()`），引入 asyncio 会破坏现有 tick 节奏
- **`time.ticks_ms()` + `time.ticks_diff()`**：ESP32 的 `ticks_ms` 会溢出，必须用 `ticks_diff` 做差值，不能直接相减
- **引脚释放**：probe.py 探测后必须 `Pin(n, Pin.IN)` 释放引脚，否则 BSM 初始化时引脚模式冲突
- **ESP32 的 JSON 状态/事件消息用 `unit_id` 自标识，并携带 `ts`**：使用 `clock.unix_time()` 统一处理 MicroPython 纪元，非 `ticks_ms`；NTP 重试与校时后快照沿用 `clock.py` / `main.py` 的入口

### 云端 / PWA

- 灯状态机固定只有 `OFF`（熄灭）、`ON`（常亮）、`BLINKING`（闪烁）三个状态；执行失败通过任务错误回执报告，不得新增 `ERROR` / “灯光异常”等状态。
- 点击“闪烁”默认持续闪烁，直到收到开灯、关灯或调色命令；不得默认设置停止时间或有限次数。只有明确指定次数时才有限闪烁。
- Python 脚本与测试使用 `uv run python` / `uv run python -m pytest`，API 沿用 Makefile 的 `uv run uvicorn`；不手动激活 venv
- PWA 无构建步骤，直接编辑 `.jsx`，浏览器刷新即生效
- PWA 经 `ApiClient` 和 `CommandBus` 访问同源 API；MQTT 发布与订阅由云端服务负责
- 新增浏览器脚本时维护 `app/index.html` 加载顺序；当前使用全局脚本和 Babel standalone，不引入未经设计的第二套构建链
- API 入口负责认证和执行上下文：Cookie 会话、写请求 CSRF；`actor/persist/space_id/requester` 只由可信服务构造，模型和浏览器不能声明权限
- 人员库写入由管理员和 `SPACE_FACE_ENROLLMENT_ENABLED` 控制，兼容旧 `GO2_FACE_ENROLLMENT_ENABLED`，缺省为开启；实际凭据只放本机 `.env`，照片经腾讯云登记，本地只存人员元数据

---

## 架构决策

### 1. 分布式多智能体总线：控制面 MQTT / 数据面按设备本性（核心）

**决策**：分两个面——**控制面**（发现、在线状态、能力声明 card）所有智能体统一走 MQTT（retained + LWT，单一真相）；**数据面**（实际调用）按设备本性选传输：ESP32 走 MQTT `task`/`result`，Go2 走 HTTP/SSE（视频流、地图、流式对话天然需要 HTTP）。

**原因**：天然解耦——新加一个智能体只需发布 manifest/card，其余节点自动感知；编排器宕机不影响 ESP32 硬件 tick。控制面统一保证"谁在线"只有一套真相；数据面分离让重设备（Go2）保留流式能力。

**避免**：ESP32 直接调用 HTTP API、编排器直接操作 GPIO、PWA 轮询 REST 状态；把控制面（card/在线状态）做成文件或 HTTP（必须在 MQTT retained）；让 Go2 这类 HTTP 调用**同步阻塞**编排器事件循环（须线程池+超时）。

### 2. ESP32 三层解耦（BSM / ISM / TriggerMap）

**决策**：BSM 驱动硬件只抛事件；ISM 只做状态转换契约；TriggerMap 是两者与 MQTT 的唯一接线点。

**原因**：BSM 和 ISM 不需要知道对方，可以独立测试每一层。固件接线在 TriggerMap；“声音触发闪烁”等用户教导与自主决策在云端 training/Graph，不能借接线修改恢复离线自动控灯规则。

**避免**：在 BSM 里发 MQTT 消息；在 ISM 里读 GPIO；在 TriggerMap 以外做 ISM↔BSM 的直接调用。

### 3. UNIT_CONFIGS 作为设备注册表（普适性）

**决策**：每个 unit 的引脚与探测策略声明在 `config.py` 的 `UNIT_CONFIGS` 字典里；只有智能体条目声明 manifest 元数据。`probe.py` 和 `agent_manifest.py` 按配置通用处理，不硬编码单元名称。传感器保留独立 MQTT `state/event` 供灯大脑感知，但不生成设备卡。

**原因**：对已有探测策略，新增配置条目即可让通用探测与 manifest 发布层识别。新增硬件类型仍需相应 BSM 驱动、主循环实例和 TriggerMap 接线；不能承诺任意传感器只改一行即可完整工作。

**避免**：在 `probe.py`、`agent_manifest.py`、`bsm.py` 里硬编码具体 unit 名称或引脚号。

### 4. 逐步任务循环（意图编排）

**决策**：三个云端智能体共用 `cloud/agents/common/` 的 LangGraph 运行、文件检查点、动作账本和回复契约；每轮由模型选择一个工具或最终回答，观察工具真实结果后再决定下一轮。各角色在 `cloud/agents/{steward,lamp,go2}/graph.py` 组装提示词和设备专属工具。

**后续开发约束**：智能体框架统一使用 LangChain / LangGraph。新增智能体或调整决策流程时，优先使用框架提供的模型、工具、状态图、检查点和恢复能力；不新增自研 Agent 循环、工具调用解析、记忆编排或第二套运行时。MQTT/HTTP 传输、设备安全校验与真实回执判定仍作为业务适配代码保留。

**原因**：灯完成后再让 Go2 行动等依赖关系必须由实际回执决定。三角色以 System 消息承载职责和表达规则、User 消息保留用户原话、原生 Tool 消息提供真实结果；普通聊天使用决策最后一轮的最终回复，独立动作/自主行动才按需生成回复。需要补充信息时使用 LangGraph `interrupt` 与原运行 ID 恢复，不重做已经完成的动作。

**避免**：重新建立独立的旧编排图或第二套问答入口；在传输辅助 `tools.py` 里写智能体业务逻辑。

### 5. PWA 无"订阅"UI 状态

**决策**：云端 API 在 MQTT 层订阅设备消息，PWA 通过 `/api/devices` 获取快照、通过 `/api/events`（SSE）接收变化。设备发现和显示是自动的，不存在用户手动"订阅设备"的 UI 步骤。

**原因**：原有“长按订阅”逻辑和 MQTT 订阅无关。设备先由总线发现，管理员将整机加入空间后，其智能体自动出现在空间内；空间成员配置不等于 MQTT 订阅，也不能代替设备能力或在线状态。

**避免**：重新引入 `subscribed` state、`toggleSub` 或 `localStorage` 的订阅持久化。

### 6. 灯智能体自主模式（manual / reactive）+ 用户让位窗口

**决策**：ESP32 灯桌面智能体有 `manual`（默认，不自发调光）/ `reactive`（结合新鲜传感器与上下文调光）两种模式，经管理员 `PUT /api/esp32/autonomy` 或可信活动适配切换；reactive 下用户/编排器控灯触发 `USER_HOLD_TTL=300` 秒让位，同伴控灯有独立 `PEER_HOLD_TTL=60` 秒窗口。灯自己的自主/游戏任务不能误触发用户让位；窗口内不自发覆盖用户设置。

**原因**：自主调光与用户/编排器命令并发会相互覆盖。模式开关与让位窗口支持用户介入，LLM 根据上下文选择颜色/亮度；底层参数校验和硬件 tick 仍由设备适配负责。Go2 自主模式存放于分层 runtime，运动权限使用租约与中断；灯使用自己的带 TTL 让位窗口，两者独立。

**避免**：在灯智能体里写"什么情况配什么颜色"的硬编码规则表；manual 模式误解为只能开关灯（富命令仍可控）；让位窗口逻辑依赖 Go2 的自主层（各智能体自主逻辑独立）。

### 7. Card 静态拓扑与真实设备事实分离

**决策**：`state_machine.regions` 是唯一静态状态与转换声明。Go2 的 connection / activity / life / body 分层结构和灯照明区域共用 Card 契约；实时动作门禁从已有执行适配投影，PWA 不另建状态或动作目录。

Go2 `status` 只表示云端服务在线；WebRTC 看 `state.connected/connection_state`，身体姿态看真实遥测和 `body_confirmation`。`autonomy_mode=manual|free_explore` 是配置，不等于当前 `life_state`。`accepted/executing` 不是完成，未确认姿态为 `null`，不能因连接、重启或超时凭空设为站立。休息不自动趴下，唤醒不自动起立或恢复探索。

自由探索实际启动后，`life_state=explore` 持续覆盖观察、规划和轮次间等待；具体进展沿用对话消息，不新增状态图节点或调整页面布局。互动任务接管时为 `play`，结束且探索许可仍有效时恢复 `explore`；停止、休息或断连后不得因旧任务结束重新进入探索。持续探索在轮次间没有具体 `life_task_id`，不能把该空值当作可完成的任务令牌。

**合并约束**：Go2 同 `epoch` 按 `revision` 排序，拒绝退役 epoch 晚到事件；灯物理状态与云端 `control_state/control_ts` 分别合并，控制投影不能刷新物理读数时间。超时按原 `task_id/request_id/run_id` 查询，不盲目重发物理动作。

### 8. 统一教导与可信执行链

**决策**：可训练转换与参数从 Card 派生；`agents/common/training/` 统一模型、草案、管理员确认、版本、冲突校验和执行工作流。API 是配置写入方；执行器每步重读教导版本和权限，复用灯/Go2 现有任务账本与真实回执。

场景文字由模型语义理解，不新增硬编码场景枚举；应用教导须带确切 ID/版本，临时参数覆盖不改写长期记录。人物出现事件不等于已识别身份，主人条件只能由可信身份观察满足。保存、加载确认和动作完成分开报告。旧 `/api/teachings`、binding 与 `/api/rules` 不恢复，旧 enabled 数据不自动导入执行。

### 9. 空间活动、同伴消息与决策依据

**决策**：`space/activity.py` 保存活动与有界时间线，API 发布签名 retained 当前快照；用户再次介入递增版本，旧模型运行不能重新开启已过期的活动请求。结束时只撤销活动持有的模式，并保留停止是否确认的真实结果。没有默认活动 TTL，重启检查模式归属，不重放历史动作。

`peers/` 处理签名定向消息、单技能请求与管家协调。定向消息的收到、接受、开始和最终结果分别记录；消息不能授予权限。`color_hunt` 的邀请、选色与目标由已有自主 Graph 结合当前观察决定，不硬编码延时、颜色或后续剧情；导航确认遵循真实停止和视觉复核回执。

游戏在 `peers/games/` 按游戏分文件维护规则、玩法、策略说明、可用条件和执行入口；统一注册表汇总，不在角色提示词中重复维护各游戏规则。自主社交不使用固定发送间隔、次数配额或同话题冷却，由现有 Graph 结合 traits、环境、任务和近期互动判断；保留新事件去重、活动与任务占用检查、消息期限及真实回执约束。

活动消息/动作可保存 `decision_basis`、`state_at_event`、`trigger_message_id`。决策理由与引用经 `space/decisions.py` 校验，依据只来自新鲜、同活动的真实观察/状态/用户要求；不能让模型自填观察事实。历史摘要不能当成当前事实，相邻消息也不能被 UI 推断成动作原因或成功证据。

---

## 验证方法

```bash
# 查看所有服务进程
make ps

# 查看最近会话与错误
make logs

# 追踪一个会话（也支持 AGENT_ID / RUN_ID / TASK_ID / JSON=1 / FOLLOW=1）
make trace-task ID=<session_id>

# 相关契约示例；*_checks.py 必须显式传入才能被默认 pytest 发现
uv run python -m pytest cloud/cards/region_contract_checks.py cloud/esp32/capability_chain_checks.py cloud/agents/common/training/context_checks.py -q

# 需要全量回归时，同时包含现有 *_checks.py
uv run python -m pytest cloud -o 'python_files=test_*.py *_test.py *_checks.py' -q

git diff --check

# ESP32 重启后串口应看到：
# [Probe] {'esp32_desk_light': True, 'esp32_desk_ir': False, 'esp32_desk_sound': True, ...}
# [Manifest] Published N manifests for esp32_desk

# PWA 验证：观察 /api/devices、/api/intents、/api/commands/{unit_id}、/api/events、/api/activity/*
make tunnel-url
```

**启动与配置**：API / PWA 默认监听 `127.0.0.1:8082/8081`，经自行配置的同源代理访问。`make api` 带 reload，`make api-bg` 不带；`*-bg` 会先停止匹配进程，重复执行会重启。tunnel 目标依赖预先建立的 `ssm` 隧道，`make tunnel-url` 只打印配置地址。Broker 密码文件需本机创建，ACL 从 `infra/broker/acl.example` 配置并在模板显式启用。API 与编排器共用内部服务令牌；`SSM_SESSION_SECRET` 至少 32 字符才使用持久会话签名，HTTPS 保持安全 Cookie，仅本机 HTTP 调试关闭。

**前端回归**：`scripts/check_*.cjs` 使用模拟 API/设备反馈；按各脚本头部提供 Node 依赖、Chrome 与 React UMD 路径后运行，例如 `node scripts/check_activity_frontend.cjs`。这些不是 PWA 构建步骤。

**改动验证节奏**：拆成可快速检查的局部改动，优先跑相关现有契约与回归。文档修改检查路径、命令和格式即可；不要为验证文档重启服务或触发硬件。模拟测试、真实模型质量与真机验收分别说明，不用历史报告替代当前结果。

---

## QA 边界

**可以做：**

- 在 `UNIT_CONFIGS` 新增设备条目
- 在 `TriggerMap` 新增事件→动作路由
- 在三个智能体的 `cloud/agents/*/graph.py` 调整逐步任务逻辑
- 修改 PWA UI 样式和交互（app.jsx）
- 沿用 Card、training、peers、space 和设备适配的既有入口扩展行为，并同步协议/契约

**不能做：**

- 在 BSM 里调用 MQTT 或 ISM
- 在 ISM 里读 GPIO
- 在 `probe.py` / `agent_manifest.py` 里硬编码 unit 名称
- 在 PWA 里重新引入 subscribed/toggleSub 状态
- 新增第二套 Agent 循环、状态图真相、教导存储或绕过可信身份/真实回执的执行入口
- 用 `accepted`、消息送达、模型描述或 retained 历史快照确认物理成功
- 用测试、文档验证覆盖已有教导、人员、人格、记忆或任务账本
- 为"将来可能有用"的功能写代码（MVP 原则）
