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
- 可选 `last_action` 保存最近动作终态，包含动作名、关联 ID、阶段、结果与确认依据；`body_observation_source`、`body_observed_at`、`body_telemetry_fresh` 解释身体观测来源、观测时间与反馈有效性。兼容字段 `body_telemetry_fresh` 不再按经过时间自动失效；断连、连接重置或明确电机故障仍会使反馈无效。`exploration_enabled` 表示当前探索许可，`exploration_owner` 记录该许可归属；无具体任务时，持续探索的 `life_task_id=null`、`life_owner=exploration_owner`。具体任务仍按自身 ID 和真实回执结束；一轮结束不撤销探索许可。`last_transition` 提供最近事实变化原因。

`body_observation_source=joint_posture` 表示结合当前连接的关节、IMU 和平面运动反馈检查姿态，不是仅靠高度或开机默认值。当前动作阶段可由本次发送后的关节运动证据进入 `executing`；`last_action.phase=completed|recovered|rejected|error` 与 `result` 分开解释：恢复到可靠姿态不等于之前超时或取消的动作已经成功。动作目录声明 `completion=joint_posture` 时，仅在相应真实观测可用时等待完成；缺少该反馈仍返回请求接受且保持未确认。

同 epoch 按 revision 排序，旧 revision 不覆盖当前状态；相同 revision 只有事实内容一致的非 retained 心跳可刷新接收时间，不允许改写事实。新 epoch 可从较小 revision 开始。
消费者拒绝已退役 epoch 的晚到事件。`ts` 用于新鲜度，不用于代替版本排序。
离线、重连和服务重启均不代表站立或动作完成，也不重发未完成身体动作。
RPC 接受仅表示请求接受；缺少可靠完成证据时保持未确认，不解锁依赖动作。

身体动作派发前即建立本次动作的完成观察。手动按钮和模型调用在关节反馈可用时共用完成判定：本次发送后观测到动作变化，再确认目标姿态稳定；始终站立不能证明招手完成。RPC 回包超时仅记录通信诊断，保留当前连接和原动作观察，不结束物理完成等待；遥测先确认时立即返回，不再额外等待 RPC。没有可靠完成证据时按原请求 ID 保持未确认，不重发动作；真实断连、动作完成期限耗尽和取消仍按各自结果处理。晚到回包不能覆盖已经记录的物理终态。行走与导航仍要求距离、角度或目标到达及停稳证据，不能仅以稳定站姿判成功。

手动身体命令复用已有 Go2 任务注册与查询接口：超过短 HTTP 等待窗口返回 `executing` 和 `task_id`，后续查询同一任务取得完成结果；同一 `request_id` 重试不重发物理动作。缺少关节反馈时仅返回请求层回执，`accepted` 不表示物理完成。

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
| `ssm/result/{unit_id}/{task_id}` | ESP32 | `{"task_id":"...","session_id":"...","result":"executing\|ok\|cancelled\|blocked\|error\|unknown","ism_state":"ON","ts":...}`；仅接收与持久任务账本中已登记归属一致的回执 |
| `ssm/task_query/{unit_id}/{task_id}` | API | `{"task_id":"...","unit_id":"...","session_id":"..."}`；只查询原任务回执，不执行物理动作；旧固件未订阅时不会误作动作命令 |

灯命令、结果查询和回执发布／订阅使用 QoS 1。固件将待确认回执放入有界内存队列，在主循环中每轮最多发送一条；不在 MQTT 收包回调内嵌套等待 PUBACK。发送失败保留队列，重连继续发送。队列上限 32 条，最近结果缓存仍为 16 条；两者均不跨固件重启持久化，断电不能保证找回历史执行结果。

云端未确认任务每 5 秒按原 ID 查询一次，每轮最多 8 条，绝不重发动作。固件有缓存则回传原结果；有限次数闪烁仍在进行则返回 `executing`。缓存缺失且该 ID 当前未执行时返回 `unknown/error=receipt_unavailable/execution_active=false`，同时缓存该结果以拦截迟到的重复命令。云端保留历史结果未知，仅解除该任务的执行占用；不能因普通超时、空闲画面或其他任务成功解除占用。迟到的真实终态仍可补齐未知结果，迟到的 `executing` 不得覆盖已确认的不再执行。PWA 收到此明确结果后结束等待，显示历史结果无法确认；未知结果仍不计为教导完成或动作成功。

任务消息可附带 `trace_id`（用户任务根 ID）、`run_id`（发起智能体运行）和 `request_id`（同伴请求）；ESP32 在回执中回传收到的关联字段，异步 `BLINK` 完成回执也沿用同一组值。这些字段只用于观测，不参与权限判断或设备动作选择。

云端执行 `SET_COLOR` 时，显式亮度（含教导选定的亮度）保持原值；省略亮度则保留当前正亮度，当前亮度为 0 时采用现有灯契约默认亮度 30，使调色同时点亮。显式 `brightness=0` 仍表示熄灭。云端在原任务账本内记录补全后的实际参数，重试沿用原值。固件在 RGB/亮度换算后按 `WS2812_MAX_VAL=30` 限制物理输出峰值，常亮、调色和闪烁共用该上限；当前为电池供电低亮测试档。参数范围仍为 0–255，上报参数不代表限幅后的像素值。超限时三个通道同乘上限与峰值之比并取整，保持 RGB 比例至整数精度；不分别截断通道。未超限时直接输出，达到上限后继续提高亮度参数不会增加物理亮度。

> 当前固件只订阅灯任务及其回执查询主题，没有离线自动控灯规则；传感器只上报状态和事件。云端离线不触发新的自主动作，已启动的硬件 tick 继续运行。

### 灯的语义技能（控制面 Card，数据面 HTTP → MQTT）

灯 manifest 含 `SET_COLOR` 时，云端 Card builder 额外公开 `adjust_lighting(goal)`。Card 的设备级 `transport` 仍是 MQTT；该技能的 `invoke.transport` 可覆盖为受校验的同源路径 `/api/internal/esp32/{unit_id}/agent/tasks`。其余灯技能仍按 MQTT `ssm/task/{unit_id}/{task_id}` 派发。Card 校验只允许灯自身的内部语义端点接收服务令牌，禁止外部 URL 和跨 unit 路径。

编排器把定性照明目标作为 `goal` 发给灯大脑；灯规划具体 RGB、亮度和动作，并通过现有 MQTT 数据面执行。语义请求持有原 `task_id`，服务端将它关联到物理 `task_id`。与 Go2、管家一致，Graph 将 `accepted` 按动作成功处理，投影为 `result=ok` 并保留 `device_result=accepted`；任务是否终结由下面的 `final` 契约决定。`executing/timeout/unknown` 暂停后续步骤，HTTP 超时后查询 `/api/internal/esp32/{unit_id}/agent/tasks/{task_id}`，不重新提交物理命令。

普通用户调灯的模型工具只提供动作参数，不要求游戏出题使用的决策证据。游戏出题仍须引用真实观察：灯 Graph 将本轮提供给模型的空间观察保存在原运行检查点，执行时按原观察时间重新判定新鲜度，同时复核当前活动版本、用户约束和动作权限；传感器随后更新不抹除仍有效的原观察。执行前灯状态也保存在同一运行内，当前状态按轮次刷新；过期或缺失的执行前观察不用于宣称原先颜色，旧运行不能从执行后状态补造执行前事实。

工具步骤保留全部尝试和真实回执。仅有执行适配明确标记为 `execution_phase=preflight`、确定尚未派发的拒绝，才可由紧邻的同一灯动作及相同物理参数的后续真实成功解除；查询不打断该重试关系，其他动作会打断。其他目标的失败、物理失败和未确认结果保持未解决，多动作部分完成返回 `partial`，不以最后一个成功步骤判定整单成功。

管家 `read_state` 读取已有 MQTT 缓存，不主动查询硬件；返回当前空间内去重、有界的观察及 `current/stale/unknown`、观察时间和来源，过期状态不输出为当前值。读取的 `ok` 只表示查询成功；任务是否完成仍按原任务 ID 和真实回执判断。普通控制结果用可感知效果简短回应，用户询问数值时才报告颜色或亮度参数。

管家委派的中间工具步骤只返回执行事实，不逐步发布设备聊天回复。父运行收尾时，按设备汇集本轮全部步骤、原用户目标和保留原观察时间的最终设备事实，每台设备发布一次结果回复；内部状态查询不另行发言。直接设备聊天和独立自主运行保留各自回复入口。管家最后用一至两句说明全局结果及未完成边界，不复述各设备过程，不承诺未实际安排的补做任务；持续灯光效果按最后的新鲜物理状态判断。

设备页按钮收到的物理 `task_id` 可由同一浏览器会话查询 `GET /api/commands/{unit_id}/tasks/{task_id}`；账本无回执时返回 `unknown/error=edge_result_unconfirmed`，实际 `ssm/result` 到达后返回已登记结果。会话结束后任务查询权限随之撤销，查询绝不重发 MQTT 任务。

多步对话使用一个 `run_id` 关联多个 `step_id`。管家、灯和 Go2 统一通过共享 LangGraph 执行以下动作结果与任务终结契约：

| 字段 | 含义与处理 |
|------|-----------|
| 工具调用参数 `final` | Graph 专用布尔控制字段，在业务参数校验、MQTT/HTTP 派发前剥离，不改变设备参数或权限。单个动作或整批最后一个动作默认 `true`，批次中间动作和只读查询默认 `false`。需要先观察结果再规划后续步骤时必须显式填写 `false`。 |
| 工具结果 `result` | `ok/accepted` 均表示动作成功；Graph 将 `accepted` 投影为 `ok`，原状态保留在 `device_result`。失败、取消、超时和执行中状态保持原义；接受成功不伪造姿态或传感器观察。 |
| 工具结果 `final` | 每步都保存布尔值。执行器可显式声明；否则成功时沿用本次调用的终结选择，失败默认可重新规划，取消直接终结。`true` 优先于剩余调用，清空队列并进入收尾，不能再次进入模型规划或派发动作。`false` 的成功结果才能继续当前批次或进入下一轮规划。 |

同轮多个调用仍按返回顺序串行执行，每步分别校验权限、参数、期限和动作预算，并沿用原工具调用 ID 去重；前一步成功且检查点已保存后才能执行下一步。失败使这批剩余调用失效；`executing/timeout/unknown` 暂停后续执行。收尾仅允许生成一次不带业务工具的自然语言回复，不能以空回复重试重新派发动作。教导工作流也将接受结果按成功处理，并在已有步骤执行结束时返回明确终结结果；重启恢复读取原账本与检查点，不重发已接受或已成功动作。

灯设备页可按原 ID 查询 `GET /api/commands/{unit_id}/runs/{run_id}`，追问后再次发送 `chat` 并携带 `run_id` 与 `resume=true`；Go2 用原 `task_id` 查询 `/api/commands/go2/tasks/{task_id}`，补充信息走 `/api/commands/go2/tasks/{task_id}/resume`。主聊天通过 `ssm/feedback/{session_id}` 的 `question` 事件取得 `run_id`，下一次 `/api/intents` 携带 `resume_run_id`。这些查询与续接都归属原会话，重启后未确认的物理步骤不会自动重发。

三类用户入口返回可信 `trace_id`；任务查询、继续和 SSE 反馈沿用原 ID。`ssm/intent/{session_id}` 的签名正文带 `trace_id`；同伴 `request/result` 带 `trace_id` 与 `parent_run_id`，HTTP 内部任务正文带同样字段。浏览器可显示该 ID 供排查，但不能用它声明角色或权限。

新的外部用户目标由 API 签发正整数 `goal_revision`，经签名意图和认证内部 HTTP 的 `X-SSM-Goal-Revision` 传播。新目标使旧排队意图和旧 Graph 失效，模型返回、工具派发及公开回复前均复核代次；原任务的真实回执仍可补齐，不能据此恢复旧目标的后续动作。明确的 `resume_run_id` 续接只接受当前代次的原运行，缺少代次的历史用户运行只能查询。该字段不由模型或浏览器声明；`user_revision` 仍单独保护活动启动请求。摇杆每个新手势用新的 `controller_id`、从 1 开始的 `sequence` 绑定代次，后续帧沿用；失效手势的迟到零速度不能停止新目标。

### 对话过程的公开事实

运行展示沿用 `agent_progress`、`agent_reply`、`run_stage` 和 `reply_error`，通过既有 `ssm/feedback/{session_id}` 与 `/api/events` 传输；活动事实还进入既有有界时间线。`stage` 是忙碌、追问或终结控制信号，不对应一句聊天记录；没有调用、观察或实际结果时不创建过程面板。

`cloud/agents/common/progress_facts.py` 是实时事件与活动历史的共同公开投影。关联字段包括 `event_id/source_event_id`、`trace_id/run_id/parent_run_id/step_id/tool_call_id/task_id`、`sequence/model_turn` 和原始 `ts/as_of`。`agent_id` 表示执行或发言角色，`target_unit_id` 表示被调用目标。`fact_kind` 区分 observation、decision、call、result、question；`result_kind` 区分 action、query、message、activity、configuration、training 等业务结果。允许的业务参数来自既有工具/Card，`public_result` 按结果类型限深、限条数、限长度；不转发任意 context、memory、提示词、凭据或原始检查点。

可选 `decision_reason/evidence_refs` 与本次原决策同轮产生，服务端从该轮可信 `decision_space` 解析为 `decision_basis.reason/evidence`。未记录或无效的可选说明省略，不额外调用解释模型；游戏等既有强制观察要求继续校验。展示元数据在动作指纹前分离，不因解释变化再次执行动作。证据保留来源、摘要和观测时间，不能用执行后状态替代原依据。

同一调用的开始与回执更新原步骤，不同 tool call 保持独立。重放保留源身份；未知结果允许由原任务的迟到真实回执补全，陈旧阶段不能覆盖已确认结果。消息投递不表示设备动作完成；活动外层 `ok` 不覆盖成员加入失败；教导保存、加载与物理完成分别展示。因果只由明确的父子运行、触发消息和证据引用建立，不能由相邻时间或相同文本推断。

单步受阻不等于整次运行终止，后续重新规划与新任务可继续；前端不根据单个失败步骤推断整单结束。过程反馈使用统一的柔和中性色，分别表达本次尝试未执行、结果待确认和明确的任务终结。消息处理成功只表示消息送达或已回复，不投影成设备动作完成。原始诊断码可保留在回执与日志中，用户可见过程、活动详情及最终回复不展示内部错误码、HTTP 状态或异常原文；已知原因转成自然语言，未知原因使用通用未完成说明。

人格发言复用原角色最终回复和 emotion。没有最终发言的工具结束才在原 Graph 的收尾节点生成一次结果回复；Go2 自主观察/运动结果进入既有自主 Graph 的结果回复分支，不重新仲裁或导航。委托设备回复按整个协调轮聚合并保持稳定身份，子回复可复用时不再次改写。结果回复生成失败使用 `reply_error/reply_source=runtime` 表达系统反馈，不能伪装成角色说过固定台词。旧历史中的 narrated 记录只在读取展示时过滤，不修改运行资产。

管家、灯和 Go2 的执行确认、教导核对与限制说明也可保留当前性格，用简短措辞表达关照、喜欢、期待、遗憾或轻微幽默，同时说清真实结果与必要步骤。不借确认、权限或执行限制训话、催促、评价用户的认真或配合程度，也不靠索取夸奖、安慰或额外承诺推动确认；普通闲聊及用户明确邀请的玩笑仍可自然展开。草案可先回应约定再提醒核对，保存、加载和动作完成仍分别判定。这些表达指导由原提示词提供，不增加模型调用或改写已保存的人格。

### 回复情绪与教导的物理表达

模型的已有对话回复可增加可选 `emotion`，与 `text`（游戏发言为 `speech`）同轮生成。允许值为 `neutral/happy/curious/confused/surprised/angry/sad/relaxed`；缺失、null、未知名称和错误类型统一降级为 `neutral`，不拒绝其余合法回复或业务决定。纯文本回复和旧运行记录仍有效。动作收尾仍不调用业务工具，也不为选择情绪额外调用模型。

规范化后的 `emotion` 随现有 `agent_reply`、公开活动消息及相关同伴发言元数据传递；编排器沿用 `ssm/feedback/{session_id}`，浏览器沿用 `/api/events`，不增加 topic、表达工具或独立表情服务。会话、角色、活动归属和事件 ID 仍由可信服务构造；私人回复不因头像共享而广播到空间。该字段表示智能体本轮的表达情绪，不是用户情绪、设备观测或动作完成证据。

统一教导的 `definition.emotion` 可关联同一套情绪名称，旧记录缺省为 null。例如“以后你开心就做开心动作”关联 `happy` 与 Card 中已有的 `Content` 转换；灯闪烁的次数、颜色等沿用教导的动作参数，不内置情绪到动作的默认表。草案仍须管理员确认，并经过原有版本、加载与执行校验。情绪教导只在被选中时生效，不覆盖普通命令的参数或全局门禁；不能与传感器 `trigger` 混用。

灯和 Go2 的普通角色 Graph 在本轮合法最终对话决定之后、进入收尾之前，使用非 `neutral` 的 `emotion` 匹配当前设备、当前请求来源的已启用教导。唯一且没有额外 `situation` 的匹配通过已有 `apply_training` 工具执行；无匹配或匹配歧义时只保留虚拟表情，附带情境的教导仍由现有 Graph 语义选择。同次运行已应用的教导不再次追加。表达动作沿用稳定步骤 ID、检查点、动作账本与真实回执；失败或结果不确定时直接收尾，不为表达情绪另开补救动作循环。

已有自主工作流复用相同匹配与教导执行：灯在自主计划返回无工具的情绪发言时选择表达，Go2 在普通自主社交消息投递成功后执行该次表达。社交消息的投递结果和表达动作回执分别保存，不能互相替代；邀请游戏、跳过交流或投递未确认不附加情绪动作。每步仍重查当前活动、用户介入和设备可用性。

物理表达保留原执行权限和设备门禁：用户对话沿用用户委托上下文，自主 Graph 沿用自主上下文；灯自主表达仍要求 `reactive` 且不在让位窗口，Go2 仍检查状态、任务占用与运动权限。受限技能不因 `emotion` 扩大工具权限。工具已经返回 `final=true`、进入无工具收尾后新生成的情绪只用于展示，不追加动作；追问、游戏专用发言、管家自身情绪和历史消息也不触发这条设备表达链。

PWA 为新回复播放短暂情绪动画，日常眨眼和起伏在客户端运行；历史消息保留静态表情。重复消息、历史加载和断线重连不重放入场动作，活动结束清除所属临时动画。实时猜拳手势由双方锁定后开始派发的公开 `rps_dispatched/game_throw` 驱动，持续到同局同轮 `rps_result` 或本局终结，不设固定展示时间；秘密选择及其情绪不能在双方锁定前公开。情绪不决定出拳、胜负或物理动作是否完成。展示结束后恢复仍有效的情绪或日常表现，刷新后回到日常，不建立独立情绪持久化。

### 编排器

| Topic | 发布者 | 格式 |
|-------|--------|------|
| `ssm/intent/{session_id}` | FastAPI（内部） | `{"text":"...","context":{...},"goal_revision":123,"_mqtt_auth":{"alg":"HMAC-SHA256","timestamp":...,"nonce":"...","signature":"..."}}`；代次由 API 签发，签名绑定完整 topic 与规范化 payload，编排器校验时间窗并拒绝 nonce 重放 |
| `ssm/session/{session_id}/ended` | FastAPI（内部） | `{"context":{...},"reason":"logout\|expired\|revoked","_mqtt_auth":{...}}`；与 intent 使用相同 HMAC 验证，触发会话历史、任务结果与 Go2 资源清理 |
| `ssm/feedback/{session_id}` | Cloud | `{"type":"run_stage\|agent_progress\|agent_reply\|reply_error","session_id":"...","agent_id":"...","stage":"...","status":"...","run_id":"...","trace_id":"...","step_id":"...","event_id":"...","sequence":1,"text":"...","ts":...}`；过程字段由上面的公开事实投影提供。`run_stage` 只承担忙碌、追问及终结控制，原始人格发言只在 `agent_reply`，生成失败和请求拒绝用 `reply_error`。`question` 暂停原 `run_id`。API/SSE 只向同一会话转发，委托设备发言保留原设备 `agent_id`。 |
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

API 在 `/api/intents` 签名正文加入 `user_revision`，编排器启动活动时必须带原 `event_id` 和该版本调用内部受控入口。用户再次介入后版本递增，旧模型运行的启动请求被拒绝。活动过程通过同空间 `/api/events` SSE 发送 `type=activity` 事件并保留有界记录。SSE 的事件名固定为 `event: activity`，`stage`（如 `mapping_proposal`、`agreement`、`rps_result`、`search_observe`）只作为 JSON 正文的阶段字段，不能覆盖事件名，否则浏览器的 `activity` 监听器无法接收；每条带 `event_id`、`activity_id`、`ts`、`event_type`，按类型附 `unit_id`、`message_id/conversation_id`、`task_id`、`media_ref`。图片通过 HTTP 数据面取，MQTT 当前状态和设备消息中不嵌原图。

Go2 加入活动后仍配置为 `free_explore`，启动事件及活动原话从当前活动存储构造，交由既有自主 Graph 选择观察、交流、探索或等待；模式切换本身不再强制执行首次整轮探索。活动内不凭好奇心计数启动无关探索，直接开启的非活动自由探索保留原流程。已收到的协作事件优先于另开社交话题；物理任务的交接仍须遵守原任务所有权、停止确认与用户中断。

新活动的用户原话本身提供一次社交评估机会，不要求双方已满足游戏开局条件；所有成员加入结果提交后，API 唤醒灯的既有工作队列，事件只携带活动 ID 和模式版本，不伪造传感器观察。模型可交流、询问或等待，开局仍独立检查真实任务与回执；旧命令结果未确认时可以说明受阻，不能据此执行游戏。灯对新活动的无工具最终答复直接投影到现有气泡，已显式发出同伴消息时不重复发布；面向用户的答复不能冒充邀请已送达。该机会按活动 ID 去重，重启不重放旧活动要求。

活动动作记录可附 `trigger_message_id`（执行入口明确关联的同伴消息）、`state_at_event`（记录时从现有设备卡投影读取的最近反馈：`status=current|stale|unknown`、`as_of`、有界 `values`），以及灯本轮实际采用的 `decision_observation`（仅光线级别、近期声音、时间段）。这些是历史记录，不是第二套实时设备状态，也不表示单凭对话或相邻时间即可证明动作原因或完成。旧记录缺少这些字段时保持未知。`GET /api/activity/{activity_id}/events` 同时返回活动元数据 `activity`，用于恢复真实退出状态与停止确认结果。

活动消息与动作还可附 `decision_basis={reason,evidence:[{ref,kind,source,as_of,summary,status?}]}`。`reason` 是最多 400 字的选择摘要，`evidence` 最多 8 条；摘要由当前共享上下文或真实工具观察生成，不接受模型自填的观察事实。模型通过工具参数 `decision_reason` 与 `evidence_refs` 引用 `space.decision_evidence`，发送或执行前重新校验引用；未知、过期和跨活动的观察不能支持新动作。物理工具剥离这些解释参数后才下发硬件。消息气泡直接展示正文，不自动拼接决策原因；邀请简短说明邀请和必要玩法，回执校验与安全执行细节留在内部协议。PWA 在统一折叠过程面板中按需展示当时保存的来源、时间和摘要；历史依据不因当前已过期而丢失，也不代表当前状态。

活动与聊天的公开过程使用同一投影：`run_id/parent_run_id/step_id` 关联原 Graph 运行及步骤，`tool/action`、`target_unit_id`、`skill_id`、`arguments`、`requested_text`、`result_state` 保留有界公开事实。`arguments` 最多 32 个标量参数，不含上下文、凭据、模型提示或私有推理；标识最多 120 字。灯能力统一使用模型工具名 `set_led_state/set_led_color/blink_led`；活动写入、SSE 与历史读取均从 `LAMP_SKILLS` 归一化，旧文件不迁移、硬件 MQTT 命令不改变。`capability_name` 从既有灯能力声明或目标 Card 派生；同任务合并只依赖真实 task/run/step 身份，不按名称或相邻时间猜测关联。`state_at_event` 仍是当时最近反馈，不证明当前动作造成该状态。详情缺字段时保持缺失，不能补造原因或观测。

猜拳的 `type=activity,event_type=action_result,stage=rps_result` 事件在双方展示动作回执确认后附 `game_round={round,choices,mapping,winner}`：`choices` 以真实 unit_id 为键，值为 `rock|scissors|paper`；`mapping` 保存该轮对应的灯 `color={r,g,b,brightness?}` 与中文 `label`，狗的 `action=go2_sport,arguments={cmd},label`，各项同时含 `choice`；`winner` 为获胜 unit_id 或 `draw`。双方锁定后开始并发派发时，先发布 `event_type=action_progress,stage=rps_dispatched,game_throw={round,choices,mapping}`，字段与上述回合一致但不含 `winner`；它仅公开本轮将执行的拳型，不证明发送成功、物理完成或胜负。前端从该事件同时显示双方手势，直到同一 `game_instance_id`、同轮回执判决或整局终结；普通发言和内部任务不打断展示，旧轮回执不清除新轮手势。出拳台词在公开展示阶段、判决之前发布；双方锁定前不发布秘密选择，未确认回合不发布胜负。历史虚拟形象沿用回执后的公开结果，不将其当作当前物理姿态；旧游戏记录没有结构化结果时不解析自由文本补造。

灯的 MQTT 状态回执确认了持续照明设置后，在线期间以 `validity=until_changed` 表示颜色／亮度有效直到新的物理状态覆盖；不按通用观察的 60 秒时效撤销该设置。`as_of` 保留原始回执时间，retained 回放不刷新时间，离线撤销连接确认，重连须有新的实际状态。该语义贯通同伴视图、空间环境、决策依据及 `state_at_event`，不放宽传感器、Go2 定位或障碍地图的观测时效。

所有游戏的角色决策与执行均进入灯、Go2 的原 Graph；游戏模块仅协调规则、阶段、约定与真实结果。游戏执行器接受本局任务后发送 `game_started`；清理前以 `game_closing` 撤销普通发言和动作资格。清理后双方各产生一条 `stage=game_final` 的自然终局发言，不限制句数或换行，仅供展示、不发送为需继续处理的同伴消息；执行归属方最后发布一次 `game_ended`。消息或单个子任务的最终回执不能提前结束整局。生命周期保留 `game_id`、原邀请 ID `game_instance_id`、`trigger_message_id`、`task_id` 和真实 `status`，结束事件保留 `reason`。PWA 显示“本局游戏已开始／本局游戏已结束”，异常结束另列原因；邀请送达不代表已开局，动作成功不代表已送达。

内部模型错误、工具尝试和传输回执以 `context_scope=process` 保留在同一活动时间线，供用户查看过程，不进入同伴模型历史或社交发现。一个协作子任务在原设备 Graph 内处理明确失败的恢复，只以一次 `event_type=task_result,context_scope=shared` 共享最终结果；未知物理结果仍按原任务核对，不能换 ID 重做。共享结果不自动要求回复，必要约定与问题才发送定向消息。旧局关闭后，其对白和结果退出当前决策上下文；活动快照携带 `phase`、`request_consumed`、已关闭／结束的游戏实例 ID，按活动 `revision` 同步 retained 状态。设备回到自由活动，不再讨论或继续旧局，再次玩必须新邀请并接受；停止未确认保留运动限制。历史原话与过程仍可在用户时间线查看。

公共事实的 `capability_name` 优先采用已有 Card 名称，内部工具的名称由其声明模块统一解析；这不会将内部工具新增为公开可调用技能。历史读取同样投影名称，不改写原文件，前端不以原始工具 ID 作为展示兜底。

### 智能体定向消息（内部，非 retained）

游戏邀请的 `game_instance_id` 固定为其自身 `message_id`；相关回复、进度和结果继承该实例身份，普通消息不能自行声明玩法或更换本局身份。接收端及执行前复核活动和该局尚未关闭；关闭后的消息不能重新触发模型协作或物理动作。原任务的真实迟到回执仍允许保存，不因此重开游戏。

| Topic | 发布者 | 格式 |
|-------|--------|------|
| `ssm/agents/{target_unit_id}/message` | 灯、Go2、编排器所在云端进程 | HMAC 签名 JSON；正文消息含 `kind=message`、`message_id`、`conversation_id`、`reply_to`、`reply_kind`、`sender`、`target`、`content`、`deadline`、`round`，活动期间另带 `activity_id`。旧普通消息可省略 `interaction_kind`，按 `message` 处理；玩法邀请附加 `interaction_kind=game_invite` 与已登记的 `game_id`，收发两端均核对同一当前活动及双方已加入，邀请本身不启动设备动作。同一主题的 `received`、`progress`、`result` 消息引用原 `message_id`；进度带 `task_id`，最终结果带 `result` 与可选 `reason`。收信端校验签名、同空间成员、目标在线、截止时间、长度和重放。QoS 1；投递成功仅表示发出，不表示目标答应或动作完成。 |

运行期按消息 ID 跟踪 `waiting_reply`、`waiting_start`、`waiting_result` 与最终结果。接收者受理物理任务后必须回传原 `task_id`，完成后回传最终结果；一旦进度已登记任务 ID，开局检查受阻等未派发动作的最终结果也必须携带同一 ID，否则按不匹配回执拒收。猜拳因灯占用或目标观察不足不能开局时，活动时间线同步记录原因，不保持“已接收”的假等待；地图和目标定位由狗赢后的导航入口校验，不要求在判拳前已经具备导航地图。结果丢失时按原任务 ID 查询，不重新发布动作。消息不走 `request_peer_skill` 的任务网关，也不授予设备或用户权限。

普通同伴消息也按现有收发记录保留待处理对话，暂缓同一对设备另开无关话题，不阻塞关联回复、进度和最终结果。处理当前消息时，可信入口为发往原对手的回复绑定原 `reply_to/conversation_id`，继承原消息期限；模型不能替换关联或扩大权限。同一处理轮显式发信后不再自动重复发送最终答复。普通回复可以进入原会话的既有角色 Graph，按当前问题及真实新结果决定是否显式续谈；仅收到回复或回执不强制再次说话，也不调用独立确认模型。已注册玩法须通过 `game_invite` 进入对应游戏执行链，普通聊天不提供规则确认、锁拳或判胜证据。

活动工作上下文必须同时匹配所属活动和当前运行代次。Go2 自主循环停止或重新启动后，不继承上一运行的目标、决策、工具结果或观察；旧运行的晚到结果仍按原任务 ID 保存，不得覆盖新运行上下文、生成新活动事件或触发后续动作。关联同伴回复的 `activity_id` 继承原消息，不能改用发送时的新活动；原活动失效后禁止该任务继续产生新消息或动作。灯的同伴执行入口在每次新副作用前重新核验原活动归属及入口有效性，单凭当前 `reactive` 不构成旧任务继续执行的许可。设备当前状态、物理回执与持久去重账本独立保留，已有动作结果仍按原 ID 核对。

`game_invite` 必须包含经发送端校验的 `decision_basis`，该字段纳入同一 HMAC 签名；接收端按发送者当前可见上下文重新核对引用。普通消息可以省略该字段。进入活动不安排固定延迟的游戏邀请，由设备现有自主 Graph 在新事件或双方进入空闲段带来的社交机会中，结合人格 traits、环境、任务和近期互动判断是否发起；空闲机会只用于重新判断，不是观察事实或证据引用，不强制发送，也不设置固定发送间隔、次数配额或同话题冷却。观察去重、消息重放校验、活动权限、任务占用、消息期限和真实回执约束仍独立生效。各游戏的规则、玩法、策略说明与执行适配按游戏保存在 `cloud/peers/games/`，由统一注册表提供当前可玩列表。

`color_hunt`、`rps_siege` 和 `push_delivery` 均声明双向邀请，邀请发起方与角色职责分离。邀请发送、接收处理及接受开局前检查双方实时任务占用；未知、离线、用户/教导任务、物理回执未确认、导航或手动运动中不能开局。Go2 在 `life_state=explore` 但无具体 `life_task_id`、无导航/运动的轮次间可以参与，不能仅凭 `idle` 或灯的照明状态推断无任务。发起中的邀请按已有消息记录占位，交叉邀请不能启动第二局。已开局游戏不再用“双方空闲”条件拒绝自身正在执行的任务。

`color_hunt` 在任一方向达成接受后，灯依据本活动内新鲜的环境观察自行选色，开灯和设色前均须引用真实观察；没有依据就如实结束。狗邀请灯时，灯完成真实设色准备后才给出接受消息，狗按原邀请 ID 去重后执行；接受消息沿用原邀请期限，真实执行结果通过关联消息回到原邀请，不把接受等同于完成。狗重新观察并自行选候选目标，视觉导航须返回 `ok` 且目标描述匹配本局；精确接近以 `confirmation=odom_stopped_and_visual_recheck` 确认，自动降级找物则要求 `outcome=nearby`、`found=true`、`stop.result=ok` 且找物任务 ID 与降级关联一致，才确认找到；游戏不安排后续变色、返航、关灯或姿态剧情。

`push_delivery`（指挥官与送货员）已开放双向邀请，不要求安装推板。接受后角色 Graph 确认灯指挥、狗找货和配送，灯按用户要求或真实观察公布具体货物；狗复用原搜索 Graph，以原任务的 `nearby`、目标身份及停稳回执确认找到，不将找到解释为接触或送达。狗报告找到并等待灯的目的地指令，灯依据新鲜观察选择真实目的地和本单约定灯色；双方确认订单后开灯、设色并核验回执，冻结本单货物、目的地和颜色。`go2_push_cargo` 先根据同一画面的货物和目的地估算方向，必要时绕到货物背向目的地的一侧，使机器狗、货物、目的地大致共线再前进。`positioned|aligned|approached` 仅为前置步骤；`directional_push_attempted=true`、`confirmation=directional_move_and_stop` 且 `stop.result=ok` 表示朝指定方向的推送尝试已完成，本局可结束。不要求精确到位或接触后仍看见脚边货物，不强制后退复查，不凭此声称货物实际移动或已送达，`delivered=false`。观察时货物原本已在目的地附近的真实回执仍可按 `cargo_near_destination_and_stopped` 报告到位。距离用本次视觉建议或显式有限正数，不设固定步长；沿用原运动权限、教学校验、完整地图、持久去重及真实停稳。货物观察最多等待 3 秒的新鲜站立反馈，允许停稳后的遥测投影延迟，不凭停稳回执伪造姿态。邀请、角色确认、选货、找货、等待指令、灯色、位置调整、推送及结果进入原活动时间线。用户介入、灯色改变、设备断连、定位会话改变或整局期限到达会中断，旧局不重放。

配送找货阶段的 `go2_find_object` 由可信任务上下文绑定原始 `cargo`，模型只选择调用时机，不需要重填目标描述；绑定发生在共享 Graph 校验、检查点和动作去重登记前，实际目标身份及停稳仍由原工具回执核验。

游戏结束事件的 `reason` 保存退出触发原因，`stage` 保存退出阶段；`physical_stop=confirmed|unknown` 和可选 `stop_reason` 单独记录清理时的停稳证据，不能覆盖退出原因。取消旧任务时不追加模型收尾，仍须由执行归属方发布一次 `game_ended`，使页面明确展示原因、阶段及停稳未确认。活动结束中或已结束后，只允许已开局游戏补记关闭/结束事实，不恢复旧发言、决策或动作权限，不修改新活动的归属。

`rps_siege`（猜拳攻灯）沿用同一邀请与结果链，阶段顺序为接受参与、公开双方映射、确认规则、认灯定位、秘密选拳与并发展示、判胜负、兑现奖惩。接受握手只确认双方愿意参与准备，不以前置亮灯、认灯或已有映射为条件。灯的结构化提议包含三个RGB颜色和逐项颜色名称，狗的提议包含三个受动作契约支持的不同动作。执行器据此生成完整公开对应关系，动作名称沿用动作契约；双方的 `mapping_proposal` 消息先进入活动时间线，再由 `agreement` 分别记录接受或拒绝，不另行发布整份静态规则或执行流程。双方接受后发布 `terms_confirmed`，固定映射和奖惩规则，再进入认灯。规则阶段不猜测目标位置，不以尚未认灯拒绝制定规则；普通聊天不能代替正式约定。

认灯保留灯模型自选识别色的步骤，开灯及设色物理回执确认后才提供提示。用户描述灯是蓝紫色球体并会放在狗的视野内；视觉问题和狗的识别决策均带此线索，但摆放承诺、同伴自述不能代替当前相机观察。狗使用设色确认后的新画面，综合球形、蓝紫色外观线索、实际发光、当前识别色和周边特征辨认，不因色调差异单独否定候选，也不凭同色物体认定身份。识别色与出拳无关，不能要求亮灯后一直保持蓝紫色。`recognition` 记录结果；观察不足或候选不唯一则保留规则并结束，不出拳。成功后将观察支持的固定目标保存于 `target_identification`，目标描述保留位置、形状或固定标记，出拳变色不换目标；此前确认的 `canonical_terms` 映射不改写。

一把定胜负、平局重来。两个角色的选择分别保存于既有 LangGraph 检查点，不把对手当前选择传入决策上下文。双方锁定后 `reveal` 并发派发灯变色和狗动作，各自沿用稳定任务ID与原执行门禁，不等待灯回执后才派发狗动作；网络和设备响应不保证硬件精确同步。两边结果都收集后才判拳，一方失败、未知或中断不判胜、不履约。每次模型返回后重检活动归属、用户介入及期限，过期决定不得公布或继续派发。

狗赢后调用 `go2_find_object`，用本局确认的固定目标描述沿可通行路径分段接近、停稳再观察，不要求雷达目标点簇或精确测距。搜索使用原邀请关联的稳定任务 ID 和本局截止时间；只有 `result=ok`、`outcome=nearby`、`found=true`、`stop.result=ok` 且返回目标与任务 ID 匹配本局才执行 `go2_sport(cmd=Hello)`，抬前腿象征性按灯，不要求实际触碰。动作沿用原可信上下文与邀请关联的稳定请求 ID `rps_{invitation_id}_switch_off_gesture`，记录活动动作及结果；只有真实完成反馈 `result=ok` 后才发送 MQTT 关灯命令，再以关灯物理回执确认履约完成。抬前腿结果为接受、执行中、未知或失败时，以及用户介入后，均停止后续关灯。灯赢则由狗选择受当前动作契约支持的表演，等待真实完成反馈。猜拳赢家和履约结果分别保存，履约失败或中断不能抹去已确认的赢家，也不能视为物理成功；未判拳时的技术失败或到期不判胜负。

游戏使用灯输出占用与 Go2 原活动任务所有权。释放占用后灯保持原自主模式，关灯成功时旧观察和周期检查不能覆盖输出，须等新的有效事件。正常完成的游戏结果交回现有活动 Graph 判断后续探索、观察或等待；恢复以原活动和探索许可仍有效为前提，用户介入撤销活动后不能继续动作或强制恢复模式。重启不恢复旧局物理执行。消息期限取注册的 `invitation_ttl_s`（默认 300 秒，猜拳攻灯 900 秒，上限 900 秒），只有有效注册游戏邀请及由本地消息记录关联的接受、进度、结果允许该期限；普通消息保持默认 60 秒、上限 300 秒。

### 智能体同伴协调请求（内部，非 retained）

同伴对灯和 Go2 的单技能请求由 API 可信网关校验后走各自现有 MQTT task/result 或 HTTP 数据面。仅请求编排器的 `coordinate_request` 使用下列独立主题；不得伪装为 `ssm/intent/{session_id}`。请求与结果均使用与 intent 相同的 `_mqtt_auth` HMAC 封套签名（签名绑定 topic、规范化 payload、时间戳和 nonce），接收方校验签名、重放和有效期。Broker ACL 应只允许 API 账号发布 request、编排器账号发布 result；禁止终端直接发布。service token 不进入 MQTT payload。

编排器收到已验证的用户 intent 时取消尚未派发的自主协调步骤。已送达设备的动作仍按目标回执和自身停止能力处理。

| Topic | 发布者 | 格式 |
|-------|--------|------|
| `ssm/peer/request/{request_id}` | FastAPI（内部，QoS 1） | `{"request_id":"...","initiator_unit_id":"go2|esp32_desk_led","target_unit_id":"cloud_orchestrator","skill_id":"coordinate_request","params":{"message":"..."},"origin_kind":"user_delegated|agent_autonomous","origin_session_id":"...|null","actor":"admin|visitor|agent","persist":false,"space_id":"main","issued_at":... ,"expires_at":... ,"_mqtt_auth":{...}}`。发起者、角色、空间和委托来源均由 API 可信上下文填入，模型传值不可信；单跳委托，编排器执行过程中不能在同一链转发同伴请求 |
| `ssm/peer/result/{request_id}` | 云端编排器（内部，QoS 1） | `{"request_id":"...","initiator_unit_id":"...","target_unit_id":"cloud_orchestrator","result":"ok|blocked|error|timeout|cancelled|executing","response":"...","task_results":{},"ts":...,"_mqtt_auth":{...}}`。`executing` 不是完成；Go2 的设备 `accepted` 也不可折算成 `ok`。同一 `request_id` 重复投递不得重复执行物理动作，超时查询已登记结果，不盲目重发 |
| `ssm/peer/cancel/{request_id}` | FastAPI（内部，QoS 1） | `{"request_id":"...","reason":"user_takeover|session_ended|mode_changed|target_offline","ts":...,"_mqtt_auth":{...}}`。仅取消尚未派发的后续协调步骤；已送达的物理动作由目标设备处理 |
