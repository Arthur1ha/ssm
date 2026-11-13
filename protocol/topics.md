# SSM MQTT 协议契约

智能体与传感输入通过以下 topic 通信。改动 topic 格式必须同步更新本文件。

## Topic 命名规则

标识符定义见 [`identifiers.md`](identifiers.md)。要点：

```
ssm/agents/{unit_id}/{type}   # 单元级
ssm/agents/{device_id}/{type} # 整机级（status / location）
```

- `unit_id`：传输单元唯一标识，`{device_id}_{unit}`，如 `esp32_desk_led`。**所有 topic 段、payload 自标识键一律用它。**
- `device_id`：整机 / 连接，如 `esp32_desk`；用于 status / location。
- `type`：项目定义的消息类型；传感输入的业务消息只有 `state`（retained）和 `event`，智能体另可发布 `manifest` 或 `card`，整机发布 `status`。
- payload 自标识键一律为 `unit_id`。

## 核心 Topic 列表

### 在线状态（retained，LWT）

| Topic | 发布者 | 格式 |
|-------|--------|------|
| `ssm/agents/{device_id}/status` | ESP32 / Go2 | `"online"` 或 `"offline"`（设备掉线时 LWT 自动置 offline）|
| `ssm/agents/{device_id}/location` | ESP32 | `{"unit_id":"...","lng":...,"lat":...,"type":"fixed","ts":...}` |

### 设备能力（retained）

| Topic | 发布者 | 格式 |
|-------|--------|------|
| `ssm/agents/{unit_id}/manifest` | ESP32 智能体（当前为灯） | `{"unit_id": "...", "parent_id":"...", "agent_type": "actuator", "tags":[...], "capabilities": [...], "ts": ...}`；灯的新版 manifest 带 `"fsm_version":3`，云端据此选择卡片；空 payload = 单元缺席或撤销旧声明。传感器不再发布非空 manifest；新固件启动/重连时发送一次空 retained payload 清理旧声明，云端发现层也忽略旧传感器 manifest。 |
| `ssm/agents/{unit_id}/card` | Go2 / 云端编排器（retained）| 完整 AgentCard JSON；空 payload = 撤销能力声明。编排器使用内部 `unit_id=cloud_orchestrator`、`agent_type=decision`，仅进入同伴视图，不进入物理设备列表 |
| `ssm/agents/cloud_orchestrator/status` | 云端编排器（retained + LWT） | `"online"` 或 `"offline"`；同伴请求可用性以此为准，不能以保留的 card 推断在线 |

### 设备状态（retained）

| Topic | 发布者 | 格式 |
|-------|--------|------|
| `ssm/agents/{unit_id}/state` | ESP32 | 灯：`{"unit_id":"esp32_desk_led","ism":"ON","r":255,"g":200,"b":80,"brightness":180,"ts":1234567890}`；`OFF` 时 RGB/亮度全为 0；`BLINKING` 时附带 `count`，RGB/亮度是闪烁配置而非瞬时相位。旧固件的 `BRIGHT/DIM/COLOR/BLINK` 在迁移期仍可被展示。传感器继续在各自 unit_id 下发送独立的当前读数，不合并到灯的状态。 |


### Go2 分层运行快照（state schema v2）

`ssm/agents/go2/state` 由 Go2 服务 retained 发布，来自与 `GET /api/go2/connection`、
`/api/go2/connection/stream` SSE 相同的 `state_snapshot()`。字段为：

- `unit_id=go2`、`state_schema_version=2`、`epoch`、`revision`、Unix 秒 `ts`。
- `connected`、`connection_state=offline|connecting|online`；服务的 status/LWT 仍决定发现在线状态，WebRTC 控制连接由这些字段表示。
- `activity_state=active|rest|null`；`life_state=idle|play|explore|null`，休息或离线时生活区域不激活。
- `body_state` 为 12 个业务身体状态；未取得可靠反馈时为 `null`（无已确认姿态，不是额外状态节点），`body_confirmation` 区分已确认与待确认；休息保留身体事实。
- `control_mode=normal|remote` 与实际 `moving` 独立；`autonomy_mode=manual|free_explore` 是配置。自由探索实际启动后，`life_state=explore` 覆盖观察、规划、移动和轮次间等待；互动任务接管时为 `play`，结束且探索许可仍有效时回到 `explore`。休息、断连和显式停止撤销探索许可，不能只凭模式配置恢复探索。
- `pending_action`、`available_actions`、`blocked_actions`、`available_life_actions`、`life_blocked_reasons` 描述真实执行阶段和当前门禁；可附带 `life_owner`、`life_task_id` 供任务追溯。
- 可选 `last_action` 保存最近动作终态，包含动作名、关联 ID、阶段、结果与确认依据；`body_observation_source`、`body_observed_at`、`body_telemetry_fresh` 解释身体观测来源与新鲜度。`exploration_enabled` 表示当前探索许可，`exploration_owner` 记录该许可归属；无具体任务时，持续探索的 `life_task_id=null`、`life_owner=exploration_owner`。具体任务仍按自身 ID 和真实回执结束；一轮结束不撤销探索许可。`last_transition` 提供最近事实变化原因。

`body_observation_source=joint_posture` 表示结合当前连接的关节、IMU 和平面运动反馈检查姿态，不是仅靠高度或开机默认值。当前动作阶段可由本次发送后的关节运动证据进入 `executing`；`last_action.phase=completed|recovered|rejected|error` 与 `result` 分开解释：恢复到可靠姿态不等于之前超时或取消的动作已经成功。动作目录声明 `completion=joint_posture` 时，仅在相应真实观测可用时等待完成；缺少该反馈仍返回请求接受且保持未确认。

同 epoch 按 revision 排序，旧 revision 不覆盖当前状态；相同 revision 只有事实内容一致的非 retained 心跳可刷新接收时间，不允许改写事实。新 epoch 可从较小 revision 开始。
消费者拒绝已退役 epoch 的晚到事件。`ts` 用于新鲜度，不用于代替版本排序。
离线、重连和服务重启均不代表站立或动作完成，也不重发未完成身体动作。
RPC 接受仅表示请求接受；缺少可靠完成证据时保持未确认，不解锁依赖动作。

Card 的 `state_machine.regions` 是唯一静态拓扑声明，区域包含 `id`、`label`、`state_field`、
`states`、`transitions`、`views`，可用 `parent:{region,state}` 表达激活关系。
Go2 声明 connection、activity、life、body 四区；body 的 `retain_when_inactive` 保留休息期间的身体展示。
灯也通过同一结构声明自己的照明区域；状态与转换只在区域中声明，删除根层平面副本和旧卡适配。`primary_region`、区域实时动作门禁绑定、命令与自动事件转移等公共字段见 [Agent Card 契约](agent-card.md)。实时值只来自 state/SSE，不复制进静态 Card。

生活控制通过已认证 `POST /api/commands/go2` 发送
`{action:"LIFE",params:{action:"request_rest|wake|start_explore|stop_explore"}}`，
也兼容上述四个操作直接作为 action。网关转交 `/api/go2/life` 并保留可信身份，复用共享工具、控制租约与状态门禁。
开启探索保留管理员边界；休息不自动趴下，唤醒不自动起立或恢复探索。
本期没有自动唤醒条件、自然语言条件规则或用户动作组合。

### 灯云端控制展示投影（非 MQTT 物理 state）

灯的设备／Agent Card API 和 `/api/events` SSE 可在边缘照明快照外附加
`autonomy_mode=manual|reactive`、`control_state=manual|ready|holding`、
`control_hold_until`（Unix 秒或 null）和独立的严格递增变化游标 `control_ts`。
这些值来自已有灯 Agent 的模式和用户／同伴让位窗口；让位到期由现有服务投递变化。
消费者分别按物理游标与云端 `control_ts` 合并：有 `epoch/revision` 时沿用版本排序；
无版本的状态优先使用 API 为实时 MQTT 消息记录的 `_received_at`，两份快照均无接收时间时才兼容使用 `ts`。
设备重启或未校时不能使新收到的物理状态被旧 `ts` 卡住；无实时接收证据的 retained 快照不能覆盖已有实时状态。
云端投影保留原 `_received_at`，不更新物理新鲜度，
不覆盖原 `ism`、颜色、亮度或 MQTT status/LWT，也不发布到 ESP32 的 retained state topic。
静态控制区域和公共字段说明见 [Agent Card 契约](agent-card.md)。

### 传感器事件

传感器是灯智能体的感知输入，不参与 Agent Card 发现；云端灯大脑继续订阅它们的 `state/event`。PWA 通过 API/SSE 获取设备信息，不直接连接 MQTT。

| Topic | 发布者 | 格式 |
|-------|--------|------|
| `ssm/agents/{unit_id}/event` | ESP32 | 光线：`{"unit_id":"...","value":1800,"level":"NORMAL","ts":...}` |
| `ssm/agents/{unit_id}/event` | ESP32 | 声音：`{"unit_id":"...","detected":true,"ts":...}` |

ESP32 的 `ts` 为 Unix 秒。上电后先通过 NTP 校准 RTC，并按 MicroPython 端口纪元换算；校时失败期间 `ts:null` 表示时间未知，消费者不得把它当作新鲜状态。设备继续运行并重试校时，成功后重新发布 retained 快照。

### 指令下发

| Topic | 发布者 | 格式 |
|-------|--------|------|
| `ssm/task/{unit_id}/{task_id}` | Cloud | `{"task_id":"...","session_id":"...","action":"SET_STATE","params":{"state":"ON"},"actor":"admin\|visitor\|agent","persist":false,"space_id":"main","ts":1234567890}`；灯的正式命令为 `SET_STATE`（`ON/OFF`）、`SET_COLOR`（`r/g/b/brightness`）、`BLINK`（`r/g/b/count`）；v3 默认 `count=0` 持续闪烁，只有明确指定 `1..20` 次数时才自动结束。持续闪烁启动后即回传 `ok`（`ism_state=BLINKING`），有限次数则等待恢复后回传成功。不携带云端 service token，发布者由 broker ACL 限制为 API/编排器账号 |
| `ssm/result/{unit_id}/{task_id}` | ESP32 | `{"task_id":"...","session_id":"...","result":"executing\|ok\|cancelled\|blocked\|error","ism_state":"ON","ts":...}`；仅接收与持久任务账本中已登记归属一致的回执 |

任务消息可附带 `trace_id`（用户任务根 ID）、`run_id`（发起智能体运行）和 `request_id`（同伴请求）；ESP32 在回执中回传收到的关联字段，异步 `BLINK` 完成回执也沿用同一组值。这些字段只用于观测，不参与权限判断或设备动作选择。

> 当前固件只订阅灯任务主题，没有离线自动控灯规则；传感器只上报状态和事件。云端离线不触发新的自主动作，已启动的硬件 tick 继续运行。

### 灯的语义技能（控制面 Card，数据面 HTTP → MQTT）

灯 manifest 含 `SET_COLOR` 时，云端 Card builder 额外公开 `adjust_lighting(goal)`。Card 的设备级 `transport` 仍是 MQTT；该技能的 `invoke.transport` 可覆盖为受校验的同源路径 `/api/internal/esp32/{unit_id}/agent/tasks`。其余灯技能仍按 MQTT `ssm/task/{unit_id}/{task_id}` 派发。Card 校验只允许灯自身的内部语义端点接收服务令牌，禁止外部 URL 和跨 unit 路径。

编排器把定性照明目标作为 `goal` 发给灯大脑；灯规划具体 RGB、亮度和动作，并通过现有 MQTT 数据面执行。语义请求持有原 `task_id`，服务端将它关联到物理 `task_id`；`accepted` 或 `executing` 均不表示完成，只有 ESP32 `ssm/result/{unit_id}/{task_id}` 的 `ok` 才能对用户确认。HTTP 超时后查询 `/api/internal/esp32/{unit_id}/agent/tasks/{task_id}`，不重新提交物理命令。

设备页按钮收到的物理 `task_id` 可由同一浏览器会话查询 `GET /api/commands/{unit_id}/tasks/{task_id}`；无回执时返回 `executing`，实际 `ssm/result` 到达后返回最终状态。会话结束后任务查询权限随之撤销，查询绝不重发 MQTT 任务。

多步对话使用一个 `run_id` 关联多个 `step_id`。灯设备页可按原 ID 查询 `GET /api/commands/{unit_id}/runs/{run_id}`，追问后再次发送 `chat` 并携带 `run_id` 与 `resume=true`；Go2 用原 `task_id` 查询 `/api/commands/go2/tasks/{task_id}`，补充信息走 `/api/commands/go2/tasks/{task_id}/resume`。主聊天通过 `ssm/feedback/{session_id}` 的 `question` 事件取得 `run_id`，下一次 `/api/intents` 携带 `resume_run_id`。这些查询与续接都归属原会话，重启后未确认的物理步骤不会自动重发。

三类用户入口返回可信 `trace_id`；任务查询、继续和 SSE 反馈沿用原 ID。`ssm/intent/{session_id}` 的签名正文带 `trace_id`；同伴 `request/result` 带 `trace_id` 与 `parent_run_id`，HTTP 内部任务正文带同样字段。浏览器可显示该 ID 供排查，但不能用它声明角色或权限。

### 编排器

| Topic | 发布者 | 格式 |
|-------|--------|------|
| `ssm/intent/{session_id}` | FastAPI（内部） | `{"text":"...","context":{...},"_mqtt_auth":{"alg":"HMAC-SHA256","timestamp":...,"nonce":"...","signature":"..."}}`；签名绑定完整 topic 与规范化 payload，编排器校验时间窗并拒绝 nonce 重放 |
| `ssm/session/{session_id}/ended` | FastAPI（内部） | `{"context":{...},"reason":"logout\|expired\|revoked","_mqtt_auth":{...}}`；与 intent 使用相同 HMAC 验证，触发会话历史、任务结果与 Go2 资源清理 |
| `ssm/feedback/{session_id}` | Cloud | `{"session_id":"...","stage":"planning\|ack\|executing\|step\|question\|done\|partial\|failed\|denied","text":"...","status":"ok\|accepted\|executing\|blocked\|rejected\|timeout\|cancelled\|error\|needs_clarification","run_id":"...","step_id":"...","step_number":1,"tool":"...","result":{},"question":"...","ts":...}`；步骤字段只在对应 stage 出现，API/SSE 只向同一会话转发。`step` 的 `result` 是实际工具结果，`question` 暂停原 `run_id`。 |
| `ssm/decision/active` | Cloud | `"true"` 或 `"false"`；编排器兼容在线标记，当前 ESP32 固件不订阅它 |

旧 `ssm/rules/{device_id}` 规则下发已停用，当前云端不发布、固件不订阅。用户教导通过 `/api/training` 管理，由云端 Graph 和执行校验层使用。

### 共享空间要求（当前状态，retained）

| Topic | 发布者 | 格式 |
|-------|--------|------|
| `ssm/space/main/constraints/current` | 编排器（已验证用户意图，retained + QoS 1） | 签名 JSON `{space_id,revision,issued_at,constraints:[...]}`；每项含 `constraint_id`、`text`、`source_session_id`、`targets`（成员 ID 或 `*`）、`issued_at`、`expires_at`。空列表清除要求。接收端验证签名并按有效期过滤；同伴消息不能创建此类要求。 |

### 空间活动（当前状态，retained）

| Topic | 发布者 | 格式 |
|-------|--------|------|
| `ssm/space/{space_id}/activity/current` | API（已验证用户请求，retained + QoS 1） | HMAC 签名 JSON `{space_id,revision,activity,ts,_mqtt_auth}`。`activity` 包含 `activity_id`、用户原话 `context`、`started_at`、`status`（`starting/active/partial/ending`）、每设备实际模式和归属；结束时用更高 `revision` 发布 `activity:null`。活动没有 `expires_at`；旧快照不能覆盖更高版本，也不能凭 retained 快照重发物理动作。 |

API 在 `/api/intents` 签名正文加入 `user_revision`，编排器启动活动时必须带原 `event_id` 和该版本调用内部受控入口。用户再次介入后版本递增，旧模型运行的启动请求被拒绝。活动过程通过同空间 `/api/events` SSE 发送 `type=activity` 事件并保留有界记录；每条带 `event_id`、`activity_id`、`ts`、`event_type`，按类型附 `unit_id`、`message_id/conversation_id`、`task_id`、`media_ref`。图片通过 HTTP 数据面取，MQTT 当前状态和设备消息中不嵌原图。

活动动作记录可附 `trigger_message_id`（执行入口明确关联的同伴消息）、`state_at_event`（记录时从现有设备卡投影读取的最近反馈：`status=current|stale|unknown`、`as_of`、有界 `values`），以及灯本轮实际采用的 `decision_observation`（仅光线级别、近期声音、时间段）。这些是历史记录，不是第二套实时设备状态，也不表示单凭对话或相邻时间即可证明动作原因或完成。旧记录缺少这些字段时保持未知。`GET /api/activity/{activity_id}/events` 同时返回活动元数据 `activity`，用于恢复真实退出状态与停止确认结果。

活动消息与动作还可附 `decision_basis={reason,evidence:[{ref,kind,source,as_of,summary,status?}]}`。`reason` 是最多 400 字的选择摘要，`evidence` 最多 8 条；摘要由当前共享上下文或真实工具观察生成，不接受模型自填的观察事实。模型通过工具参数 `decision_reason` 与 `evidence_refs` 引用 `space.decision_evidence`，发送或执行前重新校验引用；未知、过期和跨活动的观察不能支持新动作。物理工具剥离这些解释参数后才下发硬件。PWA 展示当时保存的来源、时间和摘要；历史依据不因当前已过期而丢失，也不代表当前状态。

### 智能体定向消息（内部，非 retained）

| Topic | 发布者 | 格式 |
|-------|--------|------|
| `ssm/agents/{target_unit_id}/message` | 灯、Go2、编排器所在云端进程 | HMAC 签名 JSON；正文消息含 `kind=message`、`message_id`、`conversation_id`、`reply_to`、`reply_kind`、`sender`、`target`、`content`、`deadline`、`round`，活动期间另带 `activity_id`。旧普通消息可省略 `interaction_kind`，按 `message` 处理；玩法邀请附加 `interaction_kind=game_invite` 与已登记的 `game_id`，收发两端均核对同一当前活动及双方已加入，邀请本身不启动设备动作。同一主题的 `received`、`progress`、`result` 消息引用原 `message_id`；进度带 `task_id`，最终结果带 `result` 与可选 `reason`。收信端校验签名、同空间成员、目标在线、截止时间、长度和重放。QoS 1；投递成功仅表示发出，不表示目标答应或动作完成。 |

运行期按消息 ID 跟踪 `waiting_reply`、`waiting_start`、`waiting_result` 与最终结果。接收者受理物理任务后必须回传原 `task_id`，完成后回传最终结果。结果丢失时按原任务 ID 查询，不重新发布动作。消息不走 `request_peer_skill` 的任务网关，也不授予设备或用户权限。

`game_invite` 必须包含经发送端校验的 `decision_basis`，该字段纳入同一 HMAC 签名；接收端按发送者当前可见上下文重新核对引用。普通消息可以省略该字段。进入活动不安排固定延迟的游戏邀请，由设备现有自主 Graph 在新事件带来的社交机会中，结合人格 traits、环境、任务和近期互动判断是否发起；不设置固定发送间隔、次数配额或同话题冷却。观察去重、消息重放校验、活动权限、任务占用、消息期限和真实回执约束仍独立生效。各游戏的规则、玩法、策略说明与执行适配按游戏保存在 `cloud/peers/games/`，由统一注册表提供当前可玩列表。`color_hunt` 接受后，灯依据本活动内新鲜的环境观察自行选色，开灯和设色前均须引用真实观察；没有依据就如实结束。狗重新观察并自行选候选目标，仅视觉导航返回 `ok` 且 `confirmation=odom_stopped_and_visual_recheck` 才确认找到；游戏不安排后续变色、返航、关灯或姿态剧情。

### 智能体同伴协调请求（内部，非 retained）

同伴对灯和 Go2 的单技能请求由 API 可信网关校验后走各自现有 MQTT task/result 或 HTTP 数据面。仅请求编排器的 `coordinate_request` 使用下列独立主题；不得伪装为 `ssm/intent/{session_id}`。请求与结果均使用与 intent 相同的 `_mqtt_auth` HMAC 封套签名（签名绑定 topic、规范化 payload、时间戳和 nonce），接收方校验签名、重放和有效期。Broker ACL 应只允许 API 账号发布 request、编排器账号发布 result；禁止终端直接发布。service token 不进入 MQTT payload。

编排器收到已验证的用户 intent 时取消尚未派发的自主协调步骤。已送达设备的动作仍按目标回执和自身停止能力处理。

| Topic | 发布者 | 格式 |
|-------|--------|------|
| `ssm/peer/request/{request_id}` | FastAPI（内部，QoS 1） | `{"request_id":"...","initiator_unit_id":"go2|esp32_desk_led","target_unit_id":"cloud_orchestrator","skill_id":"coordinate_request","params":{"message":"..."},"origin_kind":"user_delegated|agent_autonomous","origin_session_id":"...|null","actor":"admin|visitor|agent","persist":false,"space_id":"main","issued_at":... ,"expires_at":... ,"_mqtt_auth":{...}}`。发起者、角色、空间和委托来源均由 API 可信上下文填入，模型传值不可信；单跳委托，编排器执行过程中不能在同一链转发同伴请求 |
| `ssm/peer/result/{request_id}` | 云端编排器（内部，QoS 1） | `{"request_id":"...","initiator_unit_id":"...","target_unit_id":"cloud_orchestrator","result":"ok|blocked|error|timeout|cancelled|executing","response":"...","task_results":{},"ts":...,"_mqtt_auth":{...}}`。`executing` 不是完成；Go2 的设备 `accepted` 也不可折算成 `ok`。同一 `request_id` 重复投递不得重复执行物理动作，超时查询已登记结果，不盲目重发 |
| `ssm/peer/cancel/{request_id}` | FastAPI（内部，QoS 1） | `{"request_id":"...","reason":"user_takeover|session_ended|mode_changed|target_offline","ts":...,"_mqtt_auth":{...}}`。仅取消尚未派发的后续协调步骤；已送达的物理动作由目标设备处理 |
