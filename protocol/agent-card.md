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

每条转移的 `src`、`dst` 必须在 `states` 中，`trigger` 和 `label` 必填；`action` 为空表示自动转移，`safety=true` 走独立安全按钮。图显示全部状态节点，只画当前态的非安全、非自环出边。`views` 可给状态和运行时占位态提供标签、说明和预览。支持 `light_ring`、`robot_pose`、`image`、`generic`；`image.src` 必须是同源绝对路径，未知类型拒绝 v1 卡，运行时缺少预览实现时显示文字。`layout` 可省略，前端使用圆形布局。

## 控件与模式

`widgets[].states=[]` 表示所有状态；`placement` 为 `media` 或 `controls`，`order` 控制同区顺序，`visible=false` 隐藏。`requires_connection` 从 `connection_field` 读连接状态；`preview_only` 仅在预览选中当前态时显示。支持的类型只有：

| 类型 | 必填字段 | 派发 |
| --- | --- | --- |
| `led_color` / `color_swatches` | `action` | `/api/commands/{unit_id}`，可选 `swatches` |
| `connection` | `status_endpoint`、`connect_action`、`disconnect_action` | 状态读取同源端点，动作走命令 API |
| `joystick` | `action` | 命令 API 串行发送速度与停止 |
| `video` | 同源 `endpoint` | 同源视频快照 |

未知 widget 会记录日志并跳过；已知 widget 缺必填字段或引用未知状态也跳过。v1 不支持 `map`，不发布无法渲染的控件。

`modes[]` 给出 `id`、`label`、`options`、同源读取端点 `get` 和命令名 `set_action`。模式切换统一走 `/api/commands/{unit_id}`，服务端仍校验管理员权限和允许值。旧卡的 `set` 字段可在迁移期间作为 `set_mode` 的回退提示。

## JSON 直读入口

`/api/devices/{unit_id}/agent` 返回缩进且可解析的 `application/json`，按身份、连接与在线、能力、状态与界面分段。空行只用于分隔顶层 JSON 字段组，不插入注释或改写数据。
