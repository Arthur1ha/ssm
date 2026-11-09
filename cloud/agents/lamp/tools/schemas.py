"""Model-visible lamp arguments; executor still validates the live domain state."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, create_model

from cloud.esp32.tool_registry import TOOL_DECLARATIONS


def _physical_schema(name: str, tool: str, params_schema: dict | None = None):
    declaration = TOOL_DECLARATIONS[tool]
    schema = declaration["params_schema"] if params_schema is None else params_schema
    fields = {}
    for key, definition in schema["properties"].items():
        annotation = (Literal[tuple(definition["enum"])] if "enum" in definition else
                      int if definition.get("type") == "integer" else str)
        required = key in schema.get("required", ()) and not (tool == "blink_led" and key == "count")
        if not required:
            annotation = annotation | None
        limits = {field: definition[source] for source, field in
                  (("minimum", "ge"), ("maximum", "le")) if source in definition}
        fields[key] = (annotation, Field(default=... if required else None, **limits))
    fields["decision_reason"] = (str | None, Field(default=None, max_length=300,
        description="本次选择的简短事实理由；游戏出题必须填写"))
    fields["evidence_refs"] = (list[str] | None, Field(default=None, max_length=8,
        description="引用当前 space.decision_evidence 的 ref；游戏出题必须含观察依据"))
    return create_model(name, __doc__=declaration["description"],
                        __config__=ConfigDict(extra="forbid", strict=True), **fields)


SetLedState = _physical_schema("SetLedState", "set_led_state")
SetLedColor = _physical_schema("SetLedColor", "set_led_color")
BlinkLed = _physical_schema("BlinkLed", "blink_led")


class ListPeers(BaseModel):
    """查询当前空间的在线同伴及公开技能。"""


class GetSpaceContext(BaseModel):
    """读取同伴、环境及有效用户要求。"""


class RequestPeerSkill(BaseModel):
    """先用 list_peers 查询，再请求同伴公开技能；仅 ok 表示完成。"""
    target_unit_id: str
    skill_id: str
    params: dict


class SendMessage(BaseModel):
    """给同空间智能体发短消息；投递成功不表示接受或完成任务。

    邀请游戏时 interaction_kind=game_invite，game_id 取当前可玩列表，
    content 用自然语言说明玩法，不暴露代号；同伴接受后才按玩法执行动作。
    由当前人格和情境决定是否邀请，不用闪灯或提前设色冒充邀请。
    """
    target: str
    content: str = Field(min_length=1, max_length=1000)
    interaction_kind: Literal["message", "game_invite"] = "message"
    game_id: str | None = Field(default=None, description="邀请时填写 playable_games 中的 ID，仅供工具使用")
    decision_reason: str | None = Field(default=None, max_length=300,
        description="游戏邀请必填：依据当前情境说明为什么此时邀请")
    evidence_refs: list[str] | None = Field(default=None, max_length=8,
        description="游戏邀请必填：引用当前 space.decision_evidence 中的 ref")


SCHEMAS = {
    "set_led_state": SetLedState,
    "set_led_color": SetLedColor,
    "blink_led": BlinkLed,
    "list_peers": ListPeers,
    "get_space_context": GetSpaceContext,
    "request_peer_skill": RequestPeerSkill,
    "send_message": SendMessage,
}


def visible_schemas(*, lighting_only: bool, card: dict | None = None) -> dict:
    from cloud.agents.lamp.state import PHYSICAL_TOOLS

    allowed = PHYSICAL_TOOLS if lighting_only else SCHEMAS.keys()
    schemas = {name: schema for name, schema in SCHEMAS.items() if name in allowed}
    if not lighting_only:
        from cloud.agents.common.training.tools import SCHEMAS as training_schemas
        schemas.update(training_schemas)
    if card is not None:
        declared = {(skill.get("invoke") or {}).get("action"): skill
                    for skill in card.get("skills", [])}
        for name in PHYSICAL_TOOLS & schemas.keys():
            action = TOOL_DECLARATIONS[name]["action"]
            if action not in declared:
                schemas.pop(name)
            else:
                schemas[name] = _physical_schema(SCHEMAS[name].__name__, name,
                                                 declared[action]["params_schema"])
    return schemas
