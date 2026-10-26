"""Go2 model client configuration."""
import os
from langchain_openai import ChatOpenAI

# ── LLM 单例 ──────────────────────────────────────────────────────

_text_llm = None
_vision_llm = None


def get_text_llm() -> ChatOpenAI:
    global _text_llm
    if _text_llm is None:
        _text_llm = ChatOpenAI(
            model=os.getenv("DEVICE_TEXT_MODEL", "deepseek-v4-flash-0731"),
            base_url=os.getenv("OPENAI_BASE_URL"),
            api_key=os.getenv("OPENAI_API_KEY"),
            temperature=0,
            timeout=30,
            max_retries=0,
        )
    return _text_llm


def get_vision_llm() -> ChatOpenAI:
    global _vision_llm
    if _vision_llm is None:
        _vision_llm = ChatOpenAI(
            model=os.getenv("GO2_VISION_MODEL", "qwen"),
            base_url=os.getenv("OPENAI_BASE_URL"),
            api_key=os.getenv("OPENAI_API_KEY"),
            temperature=0,
            timeout=30,
        )
    return _vision_llm


