"""Go2 model clients shared by device perception and agent workflows."""

from cloud.agents.common.models import get_text_model, get_vision_model


def get_text_llm():
    return get_text_model("go2")


def get_vision_llm():
    return get_vision_model()
