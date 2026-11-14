# SSM Agent Card v1

SSM Card 是 MQTT retained 控制面的能力声明。`ssm/agents/{unit_id}/card` 发布自描述卡；ESP32 的 `manifest` 由云端 builder 转成相同格式。卡片借鉴 A2A 的身份和技能分层，但 MQTT 和普通 HTTP 端点不是 A2A `supportedInterfaces`，SSM 当前不提供 A2A 协议服务。

## 版本与运行时

- `card_schema_version` 必填且为整数 `1`。未知版本不覆盖 Registry 中上一张有效卡。缺少该字段、旧 v0 卡和旧平面状态机均拒绝，不再做历史格式适配。
- `version` 必填非空字符串，表示智能体或固件版本。它不控制 Card 解析规则。
- 原始 Card 只含静态声明，不含 `online`、`state` 或 `training_capabilities`。Registry 分别保存 Card、MQTT status/LWT 和 state；`/api/devices` 与设备 SSE 在读取时合成实时快照。Go2 的快照 `online` 表示服务可达，`state.connected` 表示 WebRTC 控制连接。
- 顶层必填：`unit_id`、`device_id`、`name`、`description`、`agent_type`、`transport`、`skills`。ID 和文本字段均为非空字符串，`skills` 可为空数组。`tags`、`services`、`state_machine`、`modes`、`telemetry`、`widgets`、`suggestions` 可省略，消费者按空集合处理。

## 通信与技能

`transport.kind` 为 `mqtt` 或 `http`。MQTT 需要 `task_topic`；HTTP 需要同源 `/api/` 开头的 `endpoint`，可声明 `command_endpoint` 与 `state_stream`。卡片中供 PWA 请求的 URL 必须是同源 `/api/` 路径，不接受完整跨源 URL。它们只描述调用方式，不授予权限。

每个 skill 需要 `id`、`name`、`description`、`tags` 数组、`params_schema` 对象和 `invoke.action` 字符串。skill 用于发现能力，不意味着每个技能都在状态图中有按钮。

## 状态机

有状态机时需声明 `primary_region` 和非空的 `regions`；状态、转换、预览和布局只在区域内声明一次。区域的 `state_field` 是实时 payload 的字段名，例如灯的 `ism`、Go2 的 `body_state`。根层只放区域选择、连接、执行反馈、新鲜度和画布配置，不再保留 `state_field/states/transitions/views/layout/initial` 副本。没有状态快照时 UI 显示“状态待同步”。

每条转移的 `src`、`dst` 必须在 `states` 中，`trigger` 和 `label` 必填；`action` 为空或省略表示设备观测或任务事件驱动的自动转移，只展示，不派发命令。`safety=true` 走独立安全按钮。状态图显示全部节点及命令／自动事件关系，当前态与合法可请求出边重点显示；自环仍可作为操作入口，例如再次闪烁或关闭探索许可。`views` 可给状态和运行时占位态提供标签、说明和预览。支持 `light_ring`、`robot_pose`、`image`、`generic`；`image.src` 必须是同源绝对路径，未知类型拒绝 v1 卡，运行时缺少预览实现时显示文字。`layout` 可省略；声明坐标时应覆盖全部节点，消费者仍需根据节点数量避让，不能将部分固定坐标与另一种自动布局混排。

### 并行区域与统一页面契约

`regions` 是唯一状态机拓扑。每个区域使用相同的 `id`、`label`、`state_field`、`states`、`transitions`、`views` 结构；`parent:{region,state}` 表示激活前提，父级引用必须有效且无环。`retain_when_inactive=true` 表示父级未激活时仍展示最近观测，但不允许普通操作。灯可以只声明照明区域，Go2 可以声明连接、活动、生活和身体区域；共享 schema 不要求共享状态名或业务行为。不再接受或合成平面状态机。

- `state_machine.primary_region` 指向主区域，用于默认控件状态绑定和动作预览，不按 `body` 等固定名称判断设备。
- 区域可声明 `available_actions_field` 和 `blocked_actions_field`，分别绑定实时快照中的动作键列表和阻止原因字典。转移的 `availability_key` 默认取 `action`；例如 `action=LIFE` 的休息转移使用 `availability_key=request_rest`。
- `required_actor=admin|visitor` 是界面权限提示，服务端仍必须校验身份。`available_only=true` 表示只有实时可用列表包含该键时才展示次要入口，例如生活待命时的停止探索自环。其他不可用操作可以禁用并说明原因。
- `execution_field`、`last_execution_field` 指向实时快照中的当前请求和最近终态结果。请求包含 `command`、可选 `label`、`phase` 和关联 ID；终态还可包含 `result`、`confirmation` 和完成时间。它们不会将请求接受投影成物理姿态确认。
- `freshness_field` 指向主区域的实时布尔新鲜度字段；为 `false` 时该区域普通动作受限，旧观测可继续显示。其他区域仍使用自己的动作门禁，不能因身体遥测过期禁用唤醒或停止探索。控制面 `online`、设备连接、新鲜度与动作完成是不同事实。
- 转移可用 `completion` 描述设备完成依据。`unavailable` 表示尚未具备可靠完成确认，图中有目标节点不代表该动作可以自动串联。`trigger_condition` 是唯一转换条件文案；卡片使用方向表示结果，多条触发用“或”，同条内的必要条件用“且”。
- 可选 `state_machine.diagram={width,height,sections:[{label,top}]}` 定义白板尺寸、分隔线与小标题；区域的 `diagram_group`、`node_radius` 和归一化 `layout` 坐标决定可见节点的分组、大小和位置。这些展示字段不改变运行状态、动作参数或权限。Go2 通过 `diagram.media_position="left"` 在白板左上竖排小尺寸视频和地图缩略图，两者使用相同的 16:9 外框，内容等比容纳并可点击放大；休息、待命、互动、探索置于右上“行为”，姿态和动作置于下方；连接仍只在设备标题显示。未声明媒体位置的旧 Card 沿用顶部媒体行，避免新前端遮挡旧坐标中的节点。

Go2 的连接、任务开始／结束、运动开始／停稳和姿态恢复均声明自动边。身体命令边描述可请求的目标或动作阶段；实际状态高亮来自同一连接的真实观测和执行阶段，不由点击、RPC 接受或固定延时推断。

Go2 的 `states` 仅声明实际姿态和动作，不声明 `unknown` 或“姿态待确认”节点。没有可靠姿态时，快照中的 `body_state` 为 `null`；`body_confirmation`、新鲜度和动作账本继续决定可否执行。此时不高亮任何姿态节点，独立急停入口保持可用。

灯的主区域只有三个状态 `ism=OFF|ON|BLINKING`。新版 `fsm_version=3` 的“闪烁”默认 `count=0`，持续闪烁直到收到开灯、关灯或调色命令；边缘启动闪烁并上报 `BLINKING` 后返回 `ok`，表示已进入该状态，不等待自动结束。只有明确指定正整数次数时，才在闪完并确认恢复之前的常亮颜色或熄灭后，通过 `BLINK_DONE` 返回成功；旧 v1/v2 manifest 不再生成 Card。恢复硬件失败通过任务 `result=error` 和 `error` 字段报告，状态保留最后一次确认值，直到后续命令成功；不得增加异常状态。RGB、亮度、闪烁次数是参数，不扩展成额外状态。

灯的 `manual/reactive` 自主模式和用户让位窗口由云端维护，只通过实时快照附加 `autonomy_mode`、`control_state`、`control_hold_until` 和 `control_ts`，不再额外生成控制区域或复制照明状态机。

灯的云端字段通过 `/api/devices`、设备 API 与全局 `/api/events` SSE 的统一展示投影附加到 `state`。`control_ts` 是该投影自己的变化游标，控制事实不变时保持不变；每次配置、让位截止时间或到期状态改变时严格递增。消费者将物理 `ts` 与 `control_ts` 分别合并：新控制事件可复用旧物理时间，后续物理上报也不能清除较新的控制字段。`control_ts` 不刷新物理观测的新鲜度，`online` 仍由 MQTT status/LWT 决定；`ism`、RGB、亮度和原始 `ts` 仍来自边缘。该投影不会成为第二份物理状态存储或写入 `ssm/agents/{unit_id}/state`。

## Go2 人物与身份观察

Go2 的 `vision.person_presence`、`vision.face_presence` 和 `vision.owner_recognized` 使用新鲜相机观察。
检测帧的 `ts` 为采集时间，`source_frame_id` / `camera_session_id` 标识原始帧与连接会话；
重复帧不产生新观察，超过 5 秒的观察不能证明当前有人或当前身份。
身份匹配分达到 80 且对应本地登记人员才返回 `recognized`；无匹配返回 `unknown`（陌生人），
无人脸、多脸、小脸、过期画面和服务错误不能作为陌生人或主人的证据。
身份结果的 `ts` / `frame_id` 指向实际用于匹配的检测帧，最多有效 5 秒；人脸框连续性不能替代身份复核。
临时服务故障不改变 Card 中配置性的身份能力声明，也不改变已确认教导的能力哈希；运行时错误由身份接口报告。

## Go2 相对移动

公开 `go2_move` / `MOVE` 技能使用 `direction`、`distance_m`、`angle_deg` 和可选 `speed`。前进、后退、左移、右移必须提供 `distance_m`（0.1–5 米）；原地左转、右转必须提供 `angle_deg`（5–180 度），两个目标参数互斥。`speed` 是平移速度或转向角速度上限。删除移动的 `duration` 参数，旧定时请求拒绝，不用速度乘时间推测走距；LED 等其他技能的持续时间不变。

控制使用现有 Navigator 的速度循环、地图机身净空和停稳判定，相对目标不自动绕行。平移按初始方向上的里程计投影计算实际距离，默认到量容差 0.05 米；原地转角由朝向反馈计算。到量且停稳才返回 `ok`；受阻保留实际移动量和原因；停止未确认返回 `accepted/stop_unconfirmed`。任务超时只是上限，不是位移完成条件。Go2 最终回复经现有人格层表达，校验保留实测量、未完成的目标量和停车事实，不再承诺额外观察或移动；人格模型不可用或事实校验失败时才回退到原始实测说明。

## 控件与模式

`widgets[].states=[]` 表示所有状态；`placement` 为 `media` 或 `controls`，`order` 控制同区顺序，`visible=false` 隐藏。`requires_connection` 从 `connection_field` 读连接状态；`preview_only` 仅在预览选中当前态时显示。支持的类型只有：

| 类型 | 必填字段 | 派发 |
| --- | --- | --- |
| `led_color` / `color_swatches` | `action` | `/api/commands/{unit_id}`，可选 `swatches` |
| `connection` | `status_endpoint`、`connect_action`、`disconnect_action` | 状态读取同源端点，动作走命令 API |
| `joystick` | `action` | 命令 API 串行发送速度与停止 |
| `video` | 同源 `endpoint` | 优先读取同源 MJPEG 流，失败时回退到 `endpoint/snapshot?preview=true` 轻量快照；隐藏、断连或卸载时取消读取 |
| `map` | 同源 `endpoint` | 同源地图图像 |

摇杆可声明 `node_state`、`enter_action`、`exit_action`：选择对应节点时展开，或在已进入遥控时保持展开。节点和出入命令必须属于绑定区域的声明能力；进入命令被接受不会直接启用摇杆，仍等待实时 `enabled_when` 与动作门禁。双摇杆分别控制平移与转向，并合并为同一个速度向量；分别松手只清零对应分量，全部松手发送零速度，退出遥控发送停止命令。

Go2 点击“移动”直接完成运动准备并显示摇杆，不显示单独的进入/退出操控按钮或摇杆上方的操作说明。服务端在已有姿态、租约与连接门禁内取得 `StopMove` 确认后才发布 `control_mode=remote`；准备过程不发送非零速度，也不进入控制机身姿态的 `BalanceStand`。手动摇杆以 Sport 请求包发送 `Move(1008)` 的 `x/y/z`，松杆及失联看门狗发送 `StopMove(1003)`；每个请求有独立 ID，不等待回包阻塞后续输入。点击站立退出操控；切换到其他身体动作前先停止速度控制。自主导航保留原有无线摇杆通道。

Go2 的姿态和动作在初始白板视口内全部可见，站立连接趴卧、坐下及下方动作。节点坐标及边缘留白均在画布范围内，默认无需平移寻找动作；默认显示状态转移连线。视频和地图从缩略图放大为页面内的有界浮窗，保持标题和对话入口可见，不调用全屏或遮罩整个页面。

区域可声明 `prepare_action:{via:"standing",action:"SPORT"}`。已确认坐姿或趴卧时，点击站立仅选择当前姿态对应的起身命令；点击以站立为起点的其他动作，通过 `SPORT` 的 `cmd` 与 `prepare_standing:true` 请求服务端准备。服务端 LangGraph 先执行起身，并等待同一连接、同一物理请求的站立完成回执，再发送目标动作。准备失败、姿态过期或急停均中止后续步骤。手动命令的 `dispatch_only` 只改变结果等待方式，不取消页面对已知起始姿态的选择。

未知 widget 会记录日志并跳过；已知 widget 缺必填字段或引用未知状态也跳过。控件可用 `state_field` 绑定指定区域字段，默认使用主区；`enabled_when` 为字段到 JSON 标量或标量数组的字典，逐项 AND 匹配，不支持表达式执行。`available_actions_field`、`blocked_actions_field`、`availability_key` 与区域的实时门禁语义相同。例如遥控摇杆声明 `enabled_when:{control_mode:"remote"}`，并绑定 `available_actions` 的 `Move` 键。

`modes[]` 给出 `id`、`label`、`options`、同源读取端点 `get` 和命令名 `set_action`。可选 `state_field` 绑定实时模式值，有该值时优先于初始化 GET 结果，避免其他入口改模式后本页仍高亮旧值。模式切换统一走 `/api/commands/{unit_id}`，服务端仍校验管理员权限和允许值。模式命令只声明 `set_action`，不再保留 `set` 回退字段。模式是配置，不代表设备当前活动或照明状态；灯的 `manual/reactive` 不改变其 `OFF/ON/BLINKING` 事实。

## JSON 直读入口

`/api/devices/{unit_id}/agent` 返回 Registry 收到的原始静态声明，按身份、通信、能力和界面分段，不附加运行状态。未发现原始卡时返回 404，不用离线占位设备拼装 Card。空行只分隔顶层字段组，JSON 保持可解析。

规则 API `/api/devices/{unit_id}/training` 按需从 `regions` 与 `skills` 生成能力目录；目录不回灌 Card，也不随 MQTT/SSE 的设备卡公告重复传输。稳定转换 ID 在唯一拓扑上声明，用户已保存的规则继续按这些 ID 引用。
