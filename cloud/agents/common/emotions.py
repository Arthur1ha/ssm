"""Optional avatar metadata; never a business outcome or an action."""

from typing import Annotated, Literal

from langchain_core.output_parsers import PydanticOutputParser
from pydantic import BaseModel, ConfigDict, StringConstraints, field_validator


EMOTIONS = ("neutral", "happy", "curious", "confused", "surprised", "angry", "sad", "relaxed")
EMOTION_INSTRUCTIONS = (
    "回复可附 emotion，取值 neutral、happy、curious、confused、surprised、angry、sad、relaxed。"
    "结合自己的语气、用户原话和已知真实结果选择，不确定用 neutral；"
    "情绪只用于头像展示，不代表任务成功，不修改业务决定或权限。"
    "emotion 只放在回复对象顶层，不放入业务工具参数。"
)
REPLY_INSTRUCTIONS = (
    '本步骤仅生成回复，不调用工具。输出 JSON：{"text":"给用户的自然语言回复","emotion":"neutral"}。'
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
