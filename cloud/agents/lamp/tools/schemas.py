"""Model-visible lamp arguments; executor still validates the live domain state."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class SetLedState(BaseModel):
    """开关灯。"""
    state: Literal["ON", "OFF"]
    taught_id: str | None = None


class SetLedColor(BaseModel):
    """设置 RGB 和亮度；省略亮度时保留当前值。"""
    r: int = Field(ge=0, le=255)
    g: int = Field(ge=0, le=255)
    b: int = Field(ge=0, le=255)
    brightness: int | None = Field(default=None, ge=0, le=255)
    taught_id: str | None = None


class BlinkLed(BaseModel):
    """闪烁灯环。"""
    count: int = Field(ge=1, le=20)
    r: int | None = Field(default=None, ge=0, le=255)
    g: int | None = Field(default=None, ge=0, le=255)
    b: int | None = Field(default=None, ge=0, le=255)
    taught_id: str | None = None


class ListPeers(BaseModel):
    """查询当前空间的在线同伴及公开技能。"""


class GetSpaceContext(BaseModel):
    """读取同伴、环境及有效用户要求。"""


class RequestPeerSkill(BaseModel):
    """请求同伴公开技能；接受请求不代表实体动作完成。"""
    target_unit_id: str
    skill_id: str
    params: dict


class SendMessage(BaseModel):
    """给同空间智能体发送短消息；投递成功不代表接受任务。"""
    target: str
    content: str = Field(min_length=1, max_length=1000)


class AskUser(BaseModel):
    """缺少必要信息时暂停并向用户追问。"""
    question: str = Field(min_length=1, max_length=500)


SCHEMAS = {
    "set_led_state": SetLedState,
    "set_led_color": SetLedColor,
    "blink_led": BlinkLed,
    "list_peers": ListPeers,
    "get_space_context": GetSpaceContext,
    "request_peer_skill": RequestPeerSkill,
    "send_message": SendMessage,
    "ask_user": AskUser,
}


def visible_schemas(*, lighting_only: bool) -> dict:
    from cloud.agents.lamp.state import PHYSICAL_TOOLS

    allowed = PHYSICAL_TOOLS | {"ask_user"} if lighting_only else SCHEMAS.keys()
    return {name: schema for name, schema in SCHEMAS.items() if name in allowed}
