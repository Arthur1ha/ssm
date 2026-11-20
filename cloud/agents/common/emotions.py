"""Shared expression metadata; physical expression uses confirmed teachings."""

from typing import Annotated, Literal

from langchain_core.output_parsers import PydanticOutputParser
from pydantic import BaseModel, ConfigDict, StringConstraints, field_validator


EMOTIONS = ("neutral", "happy", "curious", "confused", "surprised", "angry", "sad", "relaxed")
EMOTION_INSTRUCTIONS = (
    "回复可附 emotion，取值 neutral、happy、curious、confused、surprised、angry、sad、relaxed。"
    "结合自己的语气、用户原话和已知真实结果选择，不确定用 neutral；"
    "emotion 表示本轮自己的表达情绪，不代表用户情绪、任务成功或设备观测，不授予权限。"
    "头像沿用该情绪；已确认的情绪教导可在决策结束前经原执行器表达，"
    "没有匹配教导时不会自动追加教导动作。不要仅为输出情绪编造物理动作或回执。"
    "emotion 只放在回复对象顶层，不放入业务工具参数。"
)
REPLY_INSTRUCTIONS = (
    '本步骤仅生成回复，不调用工具。输出 JSON：{"text":"给用户的自然语言回复","emotion":"neutral"}。'
    "本步骤的 emotion 只用于呈现，不会追加物理动作。"
    + EMOTION_INSTRUCTIONS
)


def normalize_emotion(value: object) -> str:
    return value if isinstance(value, str) and value in EMOTIONS else "neutral"


class EmotionModel(BaseModel):
    """Normalize metadata before validation so invalid emotion never rejects speech."""

    emotion: str = "neutral"

    @field_validator("emotion", mode="before")
    @classmethod
    def normalize_avatar_emotion(cls, value: object) -> str:
        return normalize_emotion(value)


class EmotionalReply(EmotionModel):
    model_config = ConfigDict(extra="forbid")
    text: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
    kind: Literal["final"] = "final"


def parse_emotional_reply(value: object) -> tuple[str, str]:
    """Read a reply envelope or preserve legacy prose, without another model call."""
    if not isinstance(value, str):
        raise ValueError("invalid_reply_format")
    if value.lstrip().startswith(("{", "[", "```")):
        reply = PydanticOutputParser(pydantic_object=EmotionalReply).invoke(value)
        return reply.text, reply.emotion
    return value, "neutral"
