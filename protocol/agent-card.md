# SSM Agent Card v1

SSM Card 是 MQTT retained 控制面的能力声明。`ssm/agents/{unit_id}/card` 发布自描述卡；ESP32 的 `manifest` 由云端 builder 转成相同格式。卡片借鉴 A2A 的身份和技能分层，但 MQTT 和普通 HTTP 端点不是 A2A `supportedInterfaces`，SSM 当前不提供 A2A 协议服务。

## 版本与运行时

- `card_schema_version` 必填且为整数 `1`。未知版本不覆盖 Registry 中上一张有效卡。缺少该字段的 retained 卡按 v0 宽松读取，不假装升级到 v1。
- `version` 必填非空字符串，表示智能体或固件版本。它不控制 Card 解析规则。
- 发布者的 `online` 和 `state` 不是实时真相。Registry 从 MQTT `status`/LWT 和 `state` topic 投影它们；无状态快照时 `state={}`。Go2 的 `online` 表示云端 Go2 服务可达，`state.connected` 表示 WebRTC 控制连接。
- 顶层必填：`unit_id`、`device_id`、`name`、`description`、`agent_type`、`transport`、`skills`。ID 和文本字段均为非空字符串，`skills` 可为空数组。`tags`、`services`、`state_machine`、`modes`、`telemetry`、`widgets`、`suggestions` 可省略，消费者按空集合处理。

## 通信与技能

`transport.kind` 为 `mqtt` 或 `http`。MQTT 需要 `task_topic`；HTTP 需要同源 `/api/` 开头的 `endpoint`，可声明 `command_endpoint` 与 `state_stream`。卡片中供 PWA 请求的 URL 必须是同源 `/api/` 路径，不接受完整跨源 URL。它们只描述调用方式，不授予权限。

每个 skill 需要 `id`、`name`、`description`、`tags` 数组、`params_schema` 对象和 `invoke.action` 字符串。skill 用于发现能力，不意味着每个技能都在状态图中有按钮。

## 状态机

有状态机时需声明 `state_field`、非空且不重复的 `states`、`transitions`。`state_field` 是实时 state payload 的直接字段名，例如灯的 `ism`、Go2 的 `fsm_state`。可选 `connection_field` 是同一 payload 的布尔字段，例如 Go2 的 `connected`。`initial` 只是静态起点；没有状态快照时 UI 显示“状态待同步”。

每条转移的 `src`、`dst` 必须在 `states` 中，`trigger` 和 `label` 必填；`action` 为空或省略表示设备观测或任务事件驱动的自动转移，只展示，不派发命令。`safety=true` 走独立安全按钮。状态图显示全部节点及命令／自动事件关系，当前态与合法可请求出边重点显示；自环仍可作为操作入口，例如再次闪烁或关闭探索许可。`views` 可给状态和运行时占位态提供标签、说明和预览。支持 `light_ring`、`robot_pose`、`image`、`generic`；`image.src` 必须是同源绝对路径，未知类型拒绝 v1 卡，运行时缺少预览实现时显示文字。`layout` 可省略；声明坐标时应覆盖全部节点，消费者仍需根据节点数量避让，不能将部分固定坐标与另一种自动布局混排。

### 并行区域与统一页面契约

`regions` 是 v1 的可选扩展。每个区域使用相同的 `id`、`label`、`state_field`、`states`、`transitions`、`views` 结构；`parent:{region,state}` 表示激活前提，父级引用必须有效且无环。`retain_when_inactive=true` 表示父级未激活时仍展示最近观测，但不允许普通操作。灯可以只声明照明区域，Go2 可以声明连接、活动、生活和身体区域；共享 schema 不要求共享状态名或业务行为。没有 `regions` 的旧卡由页面消费边界适配为一个区域，原始 Card 的平面字段继续保留。

- `state_machine.primary_region` 指向主区域，用于默认控件状态绑定和动作预览，不按 `body` 等固定名称判断设备。
- 区域可声明 `available_actions_field` 和 `blocked_actions_field`，分别绑定实时快照中的动作键列表和阻止原因字典。转移的 `availability_key` 默认取 `action`；例如 `action=LIFE` 的休息转移使用 `availability_key=request_rest`。
- `required_actor=admin|visitor` 是界面权限提示，服务端仍必须校验身份。`available_only=true` 表示只有实时可用列表包含该键时才展示次要入口，例如生活待命时的停止探索自环。其他不可用操作可以禁用并说明原因。
- `execution_field`、`last_execution_field` 指向实时快照中的当前请求和最近终态结果。请求包含 `command`、可选 `label`、`phase` 和关联 ID；终态还可包含 `result`、`confirmation` 和完成时间。它们不会将请求接受投影成物理姿态确认。
- `freshness_field` 指向主区域的实时布尔新鲜度字段；为 `false` 时该区域普通动作受限，旧观测可继续显示。其他区域仍使用自己的动作门禁，不能因身体遥测过期禁用唤醒或停止探索。控制面 `online`、设备连接、新鲜度与动作完成是不同事实。
- 转移可用 `completion` 描述设备完成依据。`unavailable` 表示尚未具备可靠完成确认，图中有目标节点不代表该动作可以自动串联。`description` 可解释转移条件。
- 可选 `state_machine.diagram={width,height,sections:[{label,top}]}` 定义白板尺寸、分隔线与小标题；区域的 `diagram_group`、`node_radius` 和归一化 `layout` 坐标决定可见节点的分组、大小和位置。这些展示字段不改变运行状态、动作参数或权限。Go2 将休息、待命、互动、探索置于上方“行为”，姿态和动作置于下方；连接仍只在设备标题显示。

Go2 的连接、任务开始／结束、运动开始／停稳和姿态恢复均声明自动边。身体命令边描述可请求的目标或动作阶段；实际状态高亮来自同一连接的真实观测和执行阶段，不由点击、RPC 接受或固定延时推断。

Go2 的 `states` 仅声明实际姿态和动作，不声明 `unknown` 或“姿态待确认”节点。没有可靠姿态时，快照中的 `body_state` 为 `null`，连接在线时兼容字段 `fsm_state` 也为 `null`；`body_confirmation`、新鲜度和动作账本继续决定可否执行。此时不高亮任何姿态节点，独立急停入口保持可用。

灯的主区域只有三个状态 `ism=OFF|ON|BLINKING`（旧固件保留其原状态名）。新版 `fsm_version=3` 的“闪烁”默认 `count=0`，持续闪烁直到收到开灯、关灯或调色命令；边缘启动闪烁并上报 `BLINKING` 后返回 `ok`，表示已进入该状态，不等待自动结束。只有明确指定正整数次数时，才在闪完并确认恢复之前的常亮颜色或熄灭后，通过 `BLINK_DONE` 返回成功；v1/v2 固件仅支持有限次数，不能向其发送 `count=0`。恢复硬件失败通过任务 `result=error` 和 `error` 字段报告，状态保留最后一次确认值，直到后续命令成功；不得增加异常状态。RGB、亮度、闪烁次数是参数，不扩展成额外状态。

灯另有独立云端控制区域 `control_state=manual|ready|holding`：仅听指令、允许自动调光、临时尊重用户或同伴设置。其值由云端 Agent 的现有自主模式和让位窗口投影，区域的可选 `until_field=control_hold_until` 绑定 Unix 秒截止时间；到期后页面等待服务器同步，不自行切换状态。它不改变或替代边缘照明状态，也不写回 ESP32 的物理 retained state topic。该区域没有照明父级条件，灯熄灭时也可以保持自动调光许可。切换 `manual/reactive` 仍走模式入口，控制区自动边仅说明配置变化、外部介入与让位到期。

灯的云端字段通过 `/api/devices`、设备／Agent Card API 与全局 `/api/events` SSE 的统一展示投影附加到 `state`。`control_ts` 是该投影自己的变化游标，控制事实不变时保持不变；每次配置、让位截止时间或到期状态改变时严格递增。消费者将物理 `ts` 与 `control_ts` 分别合并：新控制事件可复用旧物理时间，后续物理上报也不能清除较新的控制字段。`control_ts` 不刷新物理观测的新鲜度，`online` 仍由 MQTT status/LWT 决定；`ism`、RGB、亮度和原始 `ts` 仍来自边缘。该投影不会成为第二份物理状态存储或写入 `ssm/agents/{unit_id}/state`。

## 控件与模式

`widgets[].states=[]` 表示所有状态；`placement` 为 `media` 或 `controls`，`order` 控制同区顺序，`visible=false` 隐藏。`requires_connection` 从 `connection_field` 读连接状态；`preview_only` 仅在预览选中当前态时显示。支持的类型只有：

| 类型 | 必填字段 | 派发 |
| --- | --- | --- |
| `led_color` / `color_swatches` | `action` | `/api/commands/{unit_id}`，可选 `swatches` |
| `connection` | `status_endpoint`、`connect_action`、`disconnect_action` | 状态读取同源端点，动作走命令 API |
| `joystick` | `action` | 命令 API 串行发送速度与停止 |
| `video` | 同源 `endpoint` | 同源视频快照 |
| `map` | 同源 `endpoint` | 同源地图图像 |

摇杆可声明 `node_state`、`enter_action`、`exit_action`：选择对应节点时展开，或在已进入遥控时保持展开。节点和出入命令必须属于绑定区域的声明能力；进入命令被接受不会直接启用摇杆，仍等待实时 `enabled_when` 与动作门禁。双摇杆分别控制平移与转向，并合并为同一个速度向量；分别松手只清零对应分量，全部松手发送零速度，退出遥控发送停止命令。

Go2 点击“移动”直接完成运动准备并显示摇杆，不显示单独的进入/退出操控按钮或摇杆上方的操作说明。服务端在已有姿态、租约与连接门禁内取得 `StopMove` 确认后才发布 `control_mode=remote`；准备过程不发送非零速度，也不进入控制机身姿态的 `BalanceStand`。手动摇杆以 Sport 请求包发送 `Move(1008)` 的 `x/y/z`，松杆及失联看门狗发送 `StopMove(1003)`；每个请求有独立 ID，不等待回包阻塞后续输入。点击站立退出操控；切换到其他身体动作前先停止速度控制。自主导航保留原有无线摇杆通道。

Go2 的姿态和动作在初始白板视口内全部可见，站立连接趴卧、坐下及下方动作。节点坐标及边缘留白均在画布范围内，默认无需平移寻找动作；默认显示状态转移连线。视频和地图从缩略图放大为页面内的有界浮窗，保持标题和对话入口可见，不调用全屏或遮罩整个页面。

区域可声明 `prepare_action:{via:"standing",action:"SPORT"}`。已确认坐姿或趴卧时，点击站立仅选择当前姿态对应的起身命令；点击以站立为起点的其他动作，通过 `SPORT` 的 `cmd` 与 `prepare_standing:true` 请求服务端准备。服务端 LangGraph 先执行起身，并等待同一连接、同一物理请求的站立完成回执，再发送目标动作。准备失败、姿态过期或急停均中止后续步骤。手动命令的 `dispatch_only` 只改变结果等待方式，不取消页面对已知起始姿态的选择。

未知 widget 会记录日志并跳过；已知 widget 缺必填字段或引用未知状态也跳过。控件可用 `state_field` 绑定指定区域字段，默认使用主区；`enabled_when` 为字段到 JSON 标量或标量数组的字典，逐项 AND 匹配，不支持表达式执行。`available_actions_field`、`blocked_actions_field`、`availability_key` 与区域的实时门禁语义相同。例如遥控摇杆声明 `enabled_when:{control_mode:"remote"}`，并绑定 `available_actions` 的 `Move` 键。

`modes[]` 给出 `id`、`label`、`options`、同源读取端点 `get` 和命令名 `set_action`。可选 `state_field` 绑定实时模式值，有该值时优先于初始化 GET 结果，避免其他入口改模式后本页仍高亮旧值。模式切换统一走 `/api/commands/{unit_id}`，服务端仍校验管理员权限和允许值。旧卡的 `set` 字段可在迁移期间作为 `set_mode` 的回退提示。模式是配置，不代表设备当前活动或照明状态；灯的 `manual/reactive` 不改变其 `OFF/ON/BLINKING` 事实。

## JSON 直读入口

`/api/devices/{unit_id}/agent` 返回缩进且可解析的 `application/json`，按身份、连接与在线、能力、状态与界面分段。空行只用于分隔顶层 JSON 字段组，不插入注释或改写数据。
