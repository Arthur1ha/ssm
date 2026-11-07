# Agent Card 与执行链路实施记录

日期：2026-11-06。对应[实施计划](2026-11-06-agent-capability-card-execution-chain.md)。

由主智能体负责接入集成与验收，三个子智能体分别实施 Go2 定义与前端、Go2 执行、智能灯链路。实现保留原命令/技能 ID、HTTP 路径、MQTT topic、签名和服务认证；没有部署或派发真机动作。软件验收与真机验收分开记录。

## 实施前状态与保护

开始时有 8 个已修改文件：`DeviceWidgets.jsx`、`FsmGraph.jsx`、`HierarchicalFsmBoard.jsx`、`app.css`、`cards/builder.py`、`region_contract_checks.py`、`schema.py`、`check_go2_frontend.cjs`。原差异保存到 `/tmp/ssm-capability-baseline/user-before.patch`，实施时保留其行为与布局修改，没有回退、暂存或提交用户修改。

基线运行计划指定的 8 组检查，结果为 **175 passed**。最初 `uv` 默认缓存不可写，后续统一使用 `UV_CACHE_DIR=/tmp/ssm-uv-cache uv run python`，未更改项目 Python 环境。

## G1：入口、身份与结果对照

| 入口 | 原调用链/缺口 | 实施后的调用链 | 兼容与完成依据 |
| --- | --- | --- | --- |
| Go2 页面挥手 | `/api/commands/go2` → `/api/go2/commands`，路由自己检查和派发 | API 转发可信会话 → `execute_command_request` → `execute_action` → WebRTC | `Hello` / SDK 1016 不变；接受和关节姿态确认分开 |
| Go2 模型挥手 | Graph → `execute_tool` → 工具自己调用设备 | 原 Graph → 工具 → 同一 `execute_action` | 保留逐轮决策、检查点、租约和结果等待 |
| Go2 固定公开技能 | `get_card_skill` → `initial_tool`，参数解析与动作定义分散 | registry 公共关联与参数 → Graph 解析 → 原执行器 | `go2_sport(cmd=Hello)`、`go2_life(action=...)` 不变 |
| Go2 公开对话 | `go2_chat.tool_name=None` 被误报未知工具 | 明确识别 `invoke.action=CHAT`，共享 schema 校验后作为自然语言目标 | 不把 CHAT 暴露成硬件工具，不扩大同伴/管家许可 |
| 灯页面闪烁 | 页面直接 `_dispatch_mqtt_task`，独有账本 | `execute_physical` 验证并预登记 → 原 MQTT publisher | `BLINK` / `count`、`task_*` 查询接口保留 |
| 灯 Graph/自主调光 | 已有执行器，但存在兼容工具裸发布支路 | 所有物理工具和兼容包装 → `execute_physical` | `adjust_lighting` 仍由灯 Graph 决定 RGB/亮度 |
| 管家闪烁 | `_invoke_skill` → 传输 helper 直接 MQTT 发布 | `_invoke_skill` → `execute_physical(wait=False)` → 原 helper → 等待回执 | helper 仍只做传输；恢复可读取持久结果 |
| 同伴闪烁 | gateway 直接 MQTT 发布/等待 | Card 参数与协作政策 → `execute_physical` → 原 MQTT transport | 自主请求带 `peer_autonomous`，不会被当作用户接管 |

控制面仍为 retained Card、status/LWT；Go2 数据面仍 HTTP/WebRTC，灯仍 `ssm/task/{unit_id}/{task_id}` / `ssm/result/{unit_id}/{task_id}`。PWA 继续使用同源 API/SSE。

可信上下文继续使用 `session_id / actor / persist / space_id / origin_kind / expires_at`，由服务认证建立。模型和浏览器参数不能替代这些字段。任务关联分别保留 `trace_id`、Graph run/step、公开请求 ID、设备 request ID，不将生命周期不同的 ID 混成一个字段。

| 结果 | 含义 |
| --- | --- |
| `accepted` / `executing` | 请求进入执行链或设备接受，尚无完成证据 |
| `ok` | 对应任务已确认完成；灯闪烁须结束并恢复 ISM，Go2 须有相关设备观测 |
| `rejected` / `blocked` | 参数/能力或运行条件/权限拒绝；无新硬件派发 |
| `error` / `cancelled` | 实际失败/已确认的取消结果；停止请求本身不冒充停稳 |
| `timeout` / `unknown` | 本次未确认，保留原 ID 查询，恢复不自动重放 |

## 定义维护位置与迁移清单（G1～G8）

| 信息类别 | 原重复位置 | 现在维护位置 | 消费者与迁移方式 |
| --- | --- | --- | --- |
| 固件动作 ID、前置姿态、目标、确认策略 | actions、工具文案、Card 预览/转移 | `cloud/go2/actions.py` 的 `ACTIONS / ActionSpec` | registry、连接适配、Card 生成读取目录；SDK 号不对外暴露 |
| 身体状态与动作预览 | Card 与前端别名、动作目录 | `actions.py` 的身体视图、`description / preview` | Card 生成页面名称/预览键；`StateVisual.jsx` 仅保留绘图资源 |
| 速度、移动时长、LED 参数 | 工具 schema、工具默认值、底层校验、摇杆 | `actions.py` 的 `VELOCITY_LIMITS / MOVE_* / LED_*` 及 schema 函数 | 工具、接收校验、WebRTC、Card 和前端同步；降低限值也同步约束默认值 |
| 云端行为名称、命令和条件 | state、Card 转移、工具/API 允许集合 | `state.py` 的 `LIFE_VIEWS / LIFE_ACTION_SPECS / LIFE_GUARDS` | `life_reasons()` 与 `life_transitions()` 同源；API 引用 `LIFE_ACTIONS` |
| 模式和行为事实、任务归属 | router `_autonomy_mode` 与 runtime | `Go2State` runtime | `service.get_autonomy_mode()` 只读投影；连接代次更新清理模式和旧任务 |
| 内部工具与公开技能 | `card.CARD_SKILLS` 和 registry 重复说明/参数 | `tools/registry.py` 的工具规格、公共子集、`CHAT_PARAMS_SCHEMA` | Card/模型工具/Graph 接收方读取；card 旧导出是直接兼容别名 |
| 设备执行和接管副作用 | router、tools 各自派发，工具反向调用 router | `tools/executor.py` 与 `service.py` | 页面和模型进入同一执行函数；service 负责模式、导航/视觉与用户介入 |
| 直接请求与真实结果 | 页面缺稳定请求身份，聊天另有任务记录 | 原 `FileEffectLedger` + `tasks.py` 关联 | 派发与晚到完成为同一请求下不同不可变证据；不另建任务运行循环 |
| 协作许可 | steward 与 peer 分散的同类分支/限值 | `cloud/peers/policy.py` | 两入口共享判断，显式保留 `audience` 差异和活动临时授权 |
| 页面拒绝原因、控件与转移绑定 | `stateMachine.js` / `RulesPage.jsx` 本地语义表 | Card `reason_labels / views / params_schema / transitions` | 页面只消费声明；旧 retained Card 无控件 schema 时保留明确兼容默认 |
| 灯固件参数/能力 | edge config、TriggerMap、云工具、Card 多份参数 | `edge/lamp_contract.py` | manifest、边缘参数检查、云工具/Graph schema、Card 同源 |
| 灯物理拓扑 | edge ISM 与当前版本云表 | `edge/ism.py` | 当前版本 Card 从真实 LED_TABLE 生成；历史版本表保留 |
| 灯物理任务 | 页面/Graph/管家/peer 多个发布入口 | `cloud/esp32/tool_executor.py` + `physical_tasks.py` | 统一验证/原始参数指纹/派发前预登记/结果关联与查询 |

保留的重复检查有明确时机：HTTP 校验服务身份；执行器检查实时姿态、任务与控制归属；发送前重新检查可能在等待期间改变的设备事实。Card 是能力与展示投影，不替代接收方授权。

## 请求重试和控制语义

Go2 `/api/go2/commands` 新增可选 `request_id / trace_id / deadline`；旧调用仍由服务端生成新 ID。PWA 聚合接口支持同名扩展，新增 `/api/commands/go2/requests/{request_id}` 转发到 `/api/go2/commands/{request_id}`。原聊天任务接口不变。

同一空间、来源、会话和 ID 的同参重试只读原记录；异参拒绝；新 ID 可再次执行相同动作。崩溃预登记、取消或超时不会触发自动重发。设备完成证据持久化后，即使另一个动作覆盖 `last_action`，旧任务查询仍保留完成结果。旧连接 epoch 的反馈不能完成新连接动作。

灯任务指纹分别保存用户原始参数与实际派发参数，避免省略亮度的重试因为当前亮度变化而误判异参。页面拒绝或重试不会取消后来启动的活动；首次有效派发才接管。`CommandBus` 保留明确结果，不再将 `unknown/error/取消` 统一显示成 `accepted`。

任务查询、聊天重试、流式接入、恢复与续租不再在验证或去重前取消活动；速度包不等待全局活动清理。急停先发零速度再后台清理活动，明确返回 `accepted / physical_stop=unknown`，不冒充停稳。

遥控仍使用双摇杆速度通道。进入遥控不声称身体已移动；松手清零对应分量；退出和急停仍走低延迟停止路径。休息/唤醒/探索规则保留，灯没有新增休息/待命状态，manual/reactive 与用户让位窗口保持原语义。

## 保留的授权差异

| 能力 | 管家受用户委托 | 设备同伴受用户委托 | 设备自主请求 |
| --- | --- | --- | --- |
| Go2 sport | 现有公开动作集合 | Hello、Dance1、Dance2 | Hello；双方属于当前活动时同伴可 Dance1/2 |
| Go2 life | 允许发现并请求，接收方仍检查管理员/任务归属 | 继续拒绝 | 拒绝 |
| Go2 bounded move / visual navigate | 允许明确用户目标/有界参数 | 同样限制 | 拒绝 |
| Go2 chat | 对话接收端可用；本次不扩大协作政策允许集合 | 同左 | 不增加自然语言绕过入口 |
| 灯 ON/OFF | 允许 | 允许 | 保留现有许可与自主可用性检查 |
| 灯 blink / adjust_lighting | 用户委托 | 用户委托 | 继续拒绝 |

技能出现在 Card 中不意味着取得授权；原服务 token、有效路由限制、消息签名、用户委托和持久化权限仍生效。

## 修改指引

- 修改已有动作说明、名称、预览或完成超时：从 `actions.py` 的对应 `ActionSpec` 开始，ID 保持不变。新增真实固件能力才需要同时补设备适配和观测策略。
- 修改移动或机身 LED 范围：修改 `actions.py` 共享限值/schema，运行 `definition_contract_checks.py`；不要再手改 Card/前端。
- 修改行为条件：从 `state.py` 的行为规格、guards 和实时条件判断开始。改变导航/视觉等副作用时再改 `service.py`。
- 修改公开技能：改 `tools/registry.py` 的公开关联或其引用的工具规格；只有独立对话入口使用 CHAT schema。内部工具不会自动公开。
- 修改页面绘图：Card 声明关联的预览 ID 不变时，只需改绘图资源；动作/状态语义不在前端重复维护。
- 修改灯能力：参数从 `edge/lamp_contract.py` 开始，物理转换从 `edge/ism.py` 开始，接线仍在 TriggerMap。改变固件版本语义时必须保留已发布旧版本的云端兼容表。
- 修改执行实现：Go2 从 `execute_action / execute_tool` 或 `service.py` 进入，灯从 `execute_physical` 进入。不要让工具回调 HTTP router，也不要绕过派发前账本。

## 验收证据

最终全部 24 个检查模块、**341 项软件检查通过**（9.16 秒）；完整软件结果见同目录 `capability-chain-evidence/software-checks.txt`。最终浏览器检查通过，结果见 [browser-checks.txt](capability-chain-evidence/browser-checks.txt)。前端浏览器使用模拟状态和媒体，未访问设备 API；截图保存为 [Go2 手机](capability-chain-evidence/go2-mobile.png)、[Go2 双摇杆](capability-chain-evidence/go2-controls-mobile.png)、[灯手机](capability-chain-evidence/lamp-mobile.png)、[地图浮窗](capability-chain-evidence/map-window.png)。

模拟请求证据（文件内均标记 `simulated: true`）：

- [闪灯完成再挥手](capability-chain-evidence/blink-then-hello-ok.json)：`blink_dispatched → blink_ok → hello_completed`。
- [闪灯超时不挥手](capability-chain-evidence/blink-then-hello-timeout.json)：`blink_dispatched → blink_timeout`，没有后续 Go2 派发。
- [成功、同 ID 重试及异参拒绝](capability-chain-evidence/lamp-retry-and-rejection.json)：相同任务只发布一次，冲突请求被拒绝。

回归覆盖动作/行为/Card 同源变更、姿态与旧反馈隔离、请求去重、真实结果恢复、过期身份、无副作用拒绝、CHAT 接入、协作许可、灯版本兼容和边缘闪烁恢复。浏览器覆盖全状态视口、转移线、有界媒体、双摇杆、停止、旧 SSE、Card 限值联动及结果显示。

本地 `.gitignore` 排除的历史 tests 并非全部可用。灯子集已在 `/tmp/ssm-lamp-baseline` 对原 HEAD 隔离验证：原 160 项中 **144 passed / 16 failed**，存在旧 freshness 时间戳、已迁移 Graph/private 接口与 mock 缺字段。与本次改动相关的 fixture 已迁移，生产校验没有为旧 mock 放宽。另有旧 Go2 personality legacy 模块引用导致历史套件无法收集。正式结果使用已纳管 `*_checks.py`，并补跑相关 API/peer 兼容套件（31 passed）与 Go2 任务/router/tool_runtime 兼容检查（50 passed，含新增执行检查）；最后 HTTP/低延迟控制回归为 127 passed，见 [go2-http-checks.txt](capability-chain-evidence/go2-http-checks.txt)，不将历史失败隐瞒为全仓所有测试通过。

## 真机与上线边界

**真机验收尚未执行。** 未确认设备就绪和活动空间，未上传 ESP32 固件、重启生产服务或刷新线上 retained Card。模拟通过不记作真机通过。

待设备就绪后按顺序验收：

1. 部署时将新 `edge/lamp_contract.py` 一并上传；README 的上传文件清单已更新。先核对 manifest 版本与生成 Card。
2. 单独执行灯开关/调光/闪烁，记录 BLINK 执行中、恢复原照明状态和完成回执。
3. 单独执行 Go2 Hello，记录真实姿态新鲜度、接受、关节反馈与完成；无可靠姿态时验证拒绝和急停可用。
4. 使用“先闪灯两次再挥手”，按 trace/task/device ID 核对两个设备的真实回执顺序。
5. 用同 ID 重试、不同参数和新 ID 验证不重发/拒绝/重新执行；记录断连、停止、双摇杆和手机界面。

回退应同时恢复相关实现、生成 Card 的声明及 ESP32 对应版本文件，避免 retained 能力与执行代码错配。没有启用并行的新旧物理派发链。
