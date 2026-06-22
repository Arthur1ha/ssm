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


class StateMachine(TypedDict):
    """智能体静态状态机拓扑。不含当前态——当前态由实时通道提供。"""

    states: list[str]
    transitions: list[FsmTransition]
    initial: NotRequired[str]   # 可选离线占位，渲染以实时当前态为准


class SkillInvoke(TypedDict):
    """技能调用信息：描述如何触发该技能。

    MQTT 类型设备：action 字段对应 ESP32 command 名称（如 SET_STATE）。
    HTTP 类型设备：action 可为空字符串，endpoint 由 transport 层给出。
    """

    action: str


class SkillDef(TypedDict):
    """单个技能定义，描述智能体能做什么及如何调用。"""

    id: str
    name: str
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
    chat_endpoint: NotRequired[str]     # mqtt 设备 HTTP 对话端点（如 /api/esp32/{unit_id}/chat），补充 mqtt 传输
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

    type: Literal["connection", "joystick", "video", "map", "color_swatches"]
    states: list[str]            # 在哪些状态下显示（空=全程）
    endpoint: NotRequired[str]   # 绑定的 transport 端点
    status_endpoint: NotRequired[str]
    auto_connect: NotRequired[bool]
    visible: NotRequired[bool]
    action: NotRequired[str]
    swatches: NotRequired[list[dict]]


class AgentCard(TypedDict):
    """智能体能力描述卡片，供编排器和 PWA 动态发现与调用。

    unit_id：唯一标识（如 esp32_desk_led）。寻址/topic/注册表 key/URL 全用它。
    online 字段为动态状态，由 registry 根据 status/LWT 或 card 消息维护。
    state 字段为动态状态，由对应 state topic 更新，builder 不填充。
    """

    unit_id: str                # 唯一标识：寻址/topic/注册表 key/URL 都用它
    device_id: NotRequired[str] # 物理设备/节点 ID；多 unit 设备共享同一 device_id
    parent_id: NotRequired[str] # 父设备 device_id，用于按 status 继承在线状态
    name: str
    description: str
    agent_type: str   # "actuator" | "sensor" | "robot" | "decision"
    online: bool
    transport: Transport
    skills: list[SkillDef]
    state: dict        # 动态状态，初始为空
    state_machine: NotRequired[StateMachine]   # 静态拓扑，无此字段则前端回退富页
    modes: NotRequired[list[ModeAxis]]          # 声明式模式轴；无则无模式开关
    telemetry: NotRequired[list[TelemetryField]] # 实时字段声明；无则无字段展示
    widgets: NotRequired[list[Widget]]           # 态内富控件；无则无摇杆/视频/地图
    suggestions: NotRequired[list[str]]          # 设备页对话快捷指令词；无则不显示
