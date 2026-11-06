"""cloud.cards.schema — Agent Card 与 Skill 的 TypedDict 类型定义。

所有跨模块共享的数据结构都在此文件声明，不含任何业务逻辑。
"""

from __future__ import annotations
from typing import Literal, NotRequired, TypedDict


class FsmTransition(TypedDict):
    """状态机一条转移：从 src 态经 trigger 命令到 dst 态。"""

    src: str        # 源状态
    dst: str        # 目标状态
    trigger: str    # 触发命令名（ESP32: CMD_BRIGHT；Go2: Move 等）
    label: str      # 人类可读按钮文案，如「开灯」
    action: NotRequired[str]   # MQTT action 名（如 SET_STATE），供前端直接派发
    params: NotRequired[dict]  # 该转移的默认 params（颜色/亮度等）
    safety: NotRequired[bool]  # 高优先级安全转移，仍走同一状态机派发路径
    graph_hidden: NotRequired[bool]  # 通过其他控件执行的动作，不在状态图里画线
    availability_key: NotRequired[str]  # 默认 action；参数化动作可绑定独立门禁键
    required_actor: NotRequired[Literal["admin", "visitor"]]  # UI 提示，权限仍由服务端校验
    available_only: NotRequired[bool]  # 不可用时不展示次要入口（例如待命时关闭探索许可）
    completion: NotRequired[str]  # 真实完成依据；unavailable 不得承诺可串联执行
    description: NotRequired[str]


class StateView(TypedDict):
    """一个状态的用户可见说明与声明式预览画面。"""

    label: str
    description: str
    type: Literal["light_ring", "robot_pose", "image", "generic"]
    pose: NotRequired[str]
    symbol: NotRequired[str]


class StateRegion(TypedDict):
    """Optional parallel/hierarchical region; runtime values stay in state."""

    id: str
    label: str
    state_field: str
    states: list[str]
    transitions: list[FsmTransition]
    views: NotRequired[dict[str, StateView]]
    layout: NotRequired[dict[str, dict[str, float]]]
    parent: NotRequired[dict[str, str]]  # {region, state} activation condition
    retain_when_inactive: NotRequired[bool]  # Show observed body facts in rest.
    available_actions_field: NotRequired[str]  # state 中的动作键列表；缺少时只依拓扑展示
    blocked_actions_field: NotRequired[str]  # state 中的 {动作键: 阻止原因}
    until_field: NotRequired[str]  # 可选 Unix 秒截止时间；到期仍等服务端状态同步
    diagram_group: NotRequired[str]  # 白板中的空间分组，不改变区域的运行语义
    node_radius: NotRequired[int]  # 白板坐标中的圆形节点半径


class StateMachine(TypedDict):
    """智能体静态状态机拓扑。不含当前态——当前态由实时通道提供。"""

    states: list[str]
    state_field: str
    connection_field: NotRequired[str]
    regions: NotRequired[list[StateRegion]]
    primary_region: NotRequired[str]  # 默认主区，控件/动作预览不依赖固定设备区域名
    execution_field: NotRequired[str]  # 当前请求，值为 {command,label,phase,...}
    last_execution_field: NotRequired[str]  # 最近终态结果，值为 {command,label,result,...}
    freshness_field: NotRequired[str]  # state 中的布尔新鲜度字段；false 时禁止普通动作
    transitions: list[FsmTransition]
    initial: NotRequired[str]   # 可选离线占位，渲染以实时当前态为准
    views: NotRequired[dict[str, StateView]]  # 状态语义与预览；前端不按设备类型硬编码
    layout: NotRequired[dict[str, dict[str, float]]]  # 相对初始画布的比例；大于 1 可延伸到平移区域
    diagram: NotRequired[dict]  # {width,height,sections:[{label,top}]}，仅影响白板布局


class SkillInvoke(TypedDict):
    """技能调用信息：描述如何触发该技能。

    MQTT 类型设备：action 字段对应 ESP32 command 名称（如 SET_STATE）。
    HTTP 类型设备：action 可为空字符串，endpoint 由 transport 层给出。
    """

    action: str
    transport: NotRequired[dict]  # optional validated per-skill transport override


class SkillDef(TypedDict):
    """单个技能定义，描述智能体能做什么及如何调用。"""

    id: str
    name: str
    description: str
    tags: list[str]
    params_schema: dict   # JSON Schema object
    invoke: SkillInvoke


class Transport(TypedDict):
    """传输层配置，描述如何与该智能体通信。

    kind = "mqtt"：通过 MQTT 发布任务消息，task_topic 为目标 topic 模板。
    kind = "http"：通过 HTTP 调用 endpoint（LLM 编排入口）；
                   command_endpoint 为 FSM transition 直接派发端点；
                   state_stream 为 SSE 实时状态流端点。
    """

    kind: Literal["mqtt", "http"]
    task_topic: NotRequired[str]        # mqtt only: ssm/task/{unit_id}/{task_id}
    endpoint: NotRequired[str]          # http: LLM 编排/对话入口（如 /api/go2/chat）
    command_endpoint: NotRequired[str]  # http: FSM 转移直接派发（如 /api/go2/commands）
    state_stream: NotRequired[str]      # http: SSE 实时状态流（如 /api/go2/connection/stream）


class ModeOption(TypedDict):
    """模式轴的一个可选值。"""

    value: str          # 设备原生模式值，如 "reactive" / "free_explore"
    label: str          # 按钮文案，如 "自动调光"
    description: str     # 一句话说明


class ModeAxis(TypedDict):
    """一个可独立设置的模式轴（如自主性）。一个设备可有 0~N 个轴。"""

    id: str             # 轴标识，如 "autonomy"
    label: str          # 轴名，如 "自主性"
    options: list[ModeOption]
    set_action: NotRequired[str]
    state_field: NotRequired[str]  # 有实时值时取代独立 GET/本地模式副本
    get: NotRequired[str]        # http: GET 端点，返回 {mode}
    set: NotRequired[str]        # http: PUT 端点，body 发 {mode: value}
    get_topic: NotRequired[str]  # mqtt: 读当前值的 topic
    set_topic: NotRequired[str]  # mqtt: 切换发布的 topic


class TelemetryField(TypedDict):
    """设备实时上报的一个字段声明，供 UI 取数展示。"""

    key: str            # 字段名，如 "fsm_state" / "body_height"
    label: str
    unit: NotRequired[str]


class Widget(TypedDict):
    """态内富控件声明，type 为 PWA 内置实现的有限集合。"""

    type: Literal["connection", "joystick", "video", "map", "color_swatches", "led_color"]
    states: list[str]            # 在哪些状态下显示（空=全程）
    state_field: NotRequired[str]  # 默认绑定 state_machine.primary_region
    placement: NotRequired[Literal["media", "controls"]]  # 语义区域，前端按屏幕尺寸排版
    order: NotRequired[int]      # 同一区域内的显示顺序
    node_state: NotRequired[str]  # 选择该节点时展开控件
    enter_action: NotRequired[str]  # 启用控件前执行的能力命令
    exit_action: NotRequired[str]  # 停止并退出控件的能力命令
    endpoint: NotRequired[str]   # 绑定的 transport 端点
    status_endpoint: NotRequired[str]
    auto_connect: NotRequired[bool]
    visible: NotRequired[bool]
    action: NotRequired[str]
    connect_action: NotRequired[str]
    disconnect_action: NotRequired[str]
    requires_connection: NotRequired[bool]
    preview_only: NotRequired[bool]
    channels: NotRequired[list[str]]
    swatches: NotRequired[list[dict]]
    enabled_when: NotRequired[dict]  # 字段→标量或标量数组，逐项 AND 匹配，无表达式执行
    available_actions_field: NotRequired[str]
    blocked_actions_field: NotRequired[str]
    availability_key: NotRequired[str]


class AgentCard(TypedDict):
    """智能体能力描述卡片，供编排器和 PWA 动态发现与调用。

    unit_id：唯一标识（如 esp32_desk_led）。寻址/topic/注册表 key/URL 全用它。
    online 字段为动态状态，由 registry 根据 status/LWT 或 card 消息维护。
    state 字段为动态状态，由对应 state topic 更新，builder 不填充。
    """

    unit_id: str                # 唯一标识：寻址/topic/注册表 key/URL 都用它
    card_schema_version: NotRequired[int]
    version: NotRequired[str]
    device_id: NotRequired[str] # 物理设备/节点 ID；多 unit 设备共享同一 device_id
    parent_id: NotRequired[str] # 父设备 device_id，用于按 status 继承在线状态
    name: str
    description: str
    agent_type: str   # "actuator" | "sensor" | "robot" | "decision"
    online: bool
    transport: Transport
    services: NotRequired[dict[str, str]]  # 云端 API 能力（如 chat_endpoint），不属于设备 transport
    tags: NotRequired[list[str]]
    skills: list[SkillDef]
    state: dict        # 动态状态，初始为空
    state_machine: NotRequired[StateMachine]   # 静态拓扑，无此字段则前端回退富页
    modes: NotRequired[list[ModeAxis]]          # 声明式模式轴；无则无模式开关
    telemetry: NotRequired[list[TelemetryField]] # 实时字段声明；无则无字段展示
    widgets: NotRequired[list[Widget]]           # 态内富控件；无则无摇杆/视频/地图
    suggestions: NotRequired[list[str]]          # 设备页对话快捷指令词；无则不显示
