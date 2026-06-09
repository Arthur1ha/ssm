"""cloud.cards.schema — Agent Card 与 Skill 的 TypedDict 类型定义。

所有跨模块共享的数据结构都在此文件声明，不含任何业务逻辑。
"""

from __future__ import annotations
from typing import Literal, NotRequired, TypedDict


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
    kind = "http"：通过 HTTP 调用 endpoint。
    """

    kind: Literal["mqtt", "http"]
    task_topic: NotRequired[str]   # mqtt only: ssm/task/{slug}/{task_id}
    endpoint: NotRequired[str]     # http only: /api/go2/chat 等


class AgentCard(TypedDict):
    """智能体能力描述卡片，供编排器和 PWA 动态发现与调用。

    online 字段为动态状态，由 registry 根据 LWT 或 card 消息维护。
    state 字段为动态状态，由对应 state topic 更新，builder 不填充。
    """

    slug: str
    name: str
    description: str
    agent_type: str   # "actuator" | "sensor" | "robot" | "decision"
    online: bool
    transport: Transport
    skills: list[SkillDef]
    state: dict        # 动态状态，初始为空
