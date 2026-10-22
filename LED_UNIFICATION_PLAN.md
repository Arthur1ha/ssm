# 灯单设备契约统一与传输边界计划

## 执行进度（仓库内）

- 已完成：v2 Card 保持 `OFF/ON/BLINKING` 与正式命令；v2 颜色控件只按新状态显示。模式留在 Card `modes`，不再展开成会被 MQTT Dispatcher 误发的技能；灯对话入口归入云端 `services`，并校验目标 unit、在线程中执行 LLM。
- 已完成：API、编排器、灯自主层的 MQTT 任务统一使用顶层 `task_id/session_id/action/params/actor/persist/space_id/ts`；注册表缓存先于 Card 到达的 status；其他设备任务不触发灯的让位或台词，灯台词只在对应任务成功后生成，并通过 API/SSE 进入 PWA 活动流。
- 已完成：灯自主层读取当前 RGB/亮度/闪烁配置，支持 `BLINK` 工具；声音、灯状态及调教规则变化可触发 `reactive` 评估，并限制评估频率。`manual` 仍为进程启动默认值。传感器继续独立 MQTT `state/event`，不生成 Agent Card。
- 待现场核对：已烧录固件版本、broker retained manifest/state、云端历史规则与设备本地规则缓存。确认都迁移后，才能删除旧 `BRIGHT/DIM/COLOR/BLINK` 灯状态兼容代码和 `SET_STATE BRIGHT/DIM` 命令别名；还需通过串口、实际 MQTT 消息和 PWA 核对闪烁恢复与任务结果。本轮未连接生产 broker，也未刷机。

## 已确认的目标

- 灯的物理状态仅为 `OFF / ON / BLINKING`。`r/g/b/brightness` 是 `ON` 的配置；`BLINKING` 是临时动作，完成后恢复先前的 `OFF` 或 `ON`。
- `manual / reactive` 是云端灯智能体的自主模式，独立于物理状态。默认 `manual`；用户显式切换后才进入 `reactive`。当前代码已如此实现，项目指引中相反的描述已更正。
- `DIM / BRIGHT` 仍可作为光线传感器的照度等级；要删除的是它们作为**灯物理状态**或 `SET_STATE` 参数的用法。
- `SET_STATE {state: ON|OFF}`、`SET_COLOR {r,g,b,brightness}`、`BLINK {r,g,b,count}` 是灯的正式命令。`SET_COLOR` 是动作，不产生 `COLOR` 状态。

## 当前传输边界

| 路径 | 现状 | 目标归属 |
| --- | --- | --- |
| PWA 控灯 | `POST /api/commands/{unit_id}` → API 校验身份和 Card → `ssm/task/{unit_id}/{task_id}` → ESP32 → MQTT `result/state` → API SSE / 页面 | HTTP 是受控入口；MQTT 是设备执行与状态总线 |
| PWA 自然语言意图 | `POST /api/intents` → MQTT `ssm/intent/{session_id}` → 编排器 → MQTT 灯任务 | HTTP 做会话与授权；编排器和设备走 MQTT |
| 灯自主感知 | ESP32 的 MQTT 传感器事件 → 云端灯智能体 → MQTT 灯任务 | 设备数据和执行都走 MQTT |
| 灯的 `manual/reactive`、聊天、调教记录 | API HTTP 端点/云端进程内调用 | 属于云端智能体的管理与对话能力，不是 ESP32 的物理命令 |
| Agent Card | ESP32 retained manifest → API 构造 Card，Card 中同时有 MQTT `transport` 和云端 HTTP 对话/模式路径 | Card 明确区分设备调用、云端管理、对话入口 |

PWA 的 HTTP 调用不表示 ESP32 改走 HTTP。浏览器继续使用带会话与 CSRF 校验的同源 API；API 负责生成 `task_id` 并发布 MQTT。HTTP 202 只表示已受理，动作完成由对应 `result` 和 `state` 判定。云端灯智能体的模式与聊天可以留在 HTTP/API 层，无需向 ESP32 增加 HTTP 服务。

MQTT 上的 manifest、status 和当前 state 使用 retained，便于新订阅者取得最近快照；task 和 result 按 `task_id` 关联，task 不 retained。现有 API 以 QoS 1 发任务，端侧已有近期任务去重逻辑；保持这项配对，因为 QoS 1 允许重复交付，不能把 MQTT 的送达确认当作硬件执行成功。传感器 event 是瞬时事件，当前 state 才是可重放快照。

## 分步改动

### 1. 收紧新版灯契约，核对迁移条件

1. 以 `edge/config.py` 的 `fsm_version=2`、`edge/ism.py` 和 `edge/trigger_map.py` 为状态源，建立 `OFF/ON/BLINKING` 与三种正式命令的契约样例，更新 `protocol/topics.md`。
2. 核对服务器 broker 中 retained manifest/state、已烧录固件，以及已保存的云端规则和 ESP32 本地规则缓存。仓库内的 `cloud/orchestrator/rules.json` 当前只使用 `SET_COLOR`，但不能据此假定运行中 broker 和设备缓存也已迁移。
3. 迁移规则中 `SET_STATE BRIGHT` 为白色 `SET_COLOR` 且 `brightness=255`，`SET_STATE DIM` 为白色 `SET_COLOR` 且 `brightness=40`；旧 `BLINK` 状态改称 `BLINKING`。只靠旧状态名无法恢复任意 RGB/亮度，必须以设备重新发布的状态为准。完成后确认 retained 状态被新固件刷新。
4. 升级设备并确认不再依赖旧命令后，删除 `edge/trigger_map.py` 的 `BRIGHT/DIM` 命令别名；删除 `cloud/cards/builder.py` 的旧五状态图和旧命令映射，将 LED 卡片技能、控件和状态图统一为新契约；删除 `app/src/pages/FsmDevicePage.jsx`、`app/src/pages/fsmWidgets.jsx`、`app/src/utils/agentMeta.js` 中的灯旧态兼容展示。光线传感器等级不变。

验收：卡片的灯状态和控件不出现 `DIM/BRIGHT/COLOR/BLINK` 作为状态；开、关、调色、闪烁及闪烁恢复在硬件、MQTT 状态、任务结果、PWA 中一致；旧固件不再向系统发布旧状态。

### 2. 明确 Card 的调用描述

1. `cloud/cards/schema.py`、`cloud/cards/builder.py` 中保持设备物理执行 `transport.kind=mqtt` 和任务 topic；将云端 `chat_endpoint`、模式读写端点标为云端服务能力，避免把混合端点解释成整张 Card 的统一传输类型。
2. 修正 `expand_mode_skills()`：当前它把 HTTP `/api/esp32/autonomy` 写进 `invoke.action`，而 `cloud/orchestrator/graph.py` 按 Card 的 MQTT 类型把该字符串当 MQTT 命令发布。先停止把 `set_autonomy` 作为可编排 MQTT 技能发布；模式仍由 Card 的 `modes` 供 PWA 使用，并通过现有授权 API 修改。如需要语音切换模式，再增加有独立鉴权的云端调用能力和按技能选择传输的 Dispatcher。
3. 对话接口只对真实挂载灯智能体的 unit 暴露；`cloud/esp32/router.py` 校验 `unit_id`，并把同步 LLM 调用移出异步事件循环。`cloud/api/intents.py` 的当前设备页对话入口保持统一会话策略。

验收：Card 中每个公开技能都能实际调用；模式切换不会被当成 ESP32 MQTT 命令；聊天不会误指向其他 ESP32 单元。

### 3. 统一任务和在线契约

1. 在 `cloud/api/intents.py`、`cloud/orchestrator/tools.py`、`cloud/esp32/tools.py` 统一任务信封：顶层 `task_id/session_id/action/params/actor/persist/space_id/ts`，保持端侧任务结果可按 `task_id` 关联。增加兼容读取窗口后，移除嵌套 `context` 的差异。
2. `cloud/cards/registry.py` 缓存尚未收到 Card 时先到达的 retained `status`，让 `offline → manifest` 和 `manifest → offline` 都得到 `online=false`。
3. `cloud/api/main.py` 只把目标为灯的外部命令交给灯智能体做让位与动作叙述，且等待对应成功结果后再叙述；`cloud/esp32/tools.py` 的 `thought` payload 补齐协议所需类型。

验收：两种 retained 消息顺序的在线值一致；不同发布入口发出的灯任务有同一信封；给其他设备下命令不会触发灯的让位或台词。

### 4. 完善灯的单设备智能体感知

1. `cloud/esp32/agent.py` 的 `_sense()` 加入当前灯的 RGB/亮度/闪烁配置，避免只凭 `ism` 决策；工具能力表与 Card 对齐，显式支持 `BLINK` 或明确不向智能体宣称这项自主能力。
2. 调整推理触发条件，使声音事件、灯状态变化、规则变化也能在 `reactive` 下触发一次评估，并修复主动决策时间戳。保留冷却和用户让位窗口，限制高频事件带来的 LLM 调用。
3. `manual` 下验证传感器事件不会触发自主控灯，但手动富命令、聊天和调教继续可用。默认值应在进程重启后仍为 `manual`。

验收：`manual` 重启安全；`reactive` 在实际上下文变化时评估，且不会因同一事件反复改灯；决策能看到灯的真实配置。

## 执行与验证顺序

每次只交付一个可独立验证的小改动：先迁移旧数据并验证新固件状态，再删旧命名；随后修 Card 调用描述、任务信封与在线注册表，最后优化灯智能体推理。每步用小范围单元/桩验证协议与状态转移，涉及硬件的步骤再用串口、MQTT retained 消息和 PWA 对照。不要把历史 `DIM/BRIGHT` 照度等级误删。
