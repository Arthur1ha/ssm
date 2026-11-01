"""Model construction preserving each role's existing environment policy."""

import os

from langchain_openai import ChatOpenAI


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def get_text_model(role: str, model_class=None):
    """Construct a role model; role is steward, lamp, or go2."""
    model_class = model_class or ChatOpenAI
    common = {
        "base_url": os.getenv("OPENAI_BASE_URL"),
        "api_key": os.getenv("OPENAI_API_KEY"),
        "temperature": 0,
    }
    if role == "steward":
        models = [m.strip() for m in os.getenv("MODEL_LIST", os.getenv("MODEL", "")).split(",") if m.strip()]
        models = models or ["deepseek-chat"]
        common.update(timeout=_int_env("LLM_TIMEOUT", 30), max_retries=_int_env("LLM_MAX_RETRIES", 1))
        max_tokens = _int_env("ORCHESTRATOR_MAX_TOKENS", 900)
        if max_tokens > 0:
            common["max_tokens"] = max_tokens
        chain = [model_class(model=model, **common) for model in models]
        return chain[0].with_fallbacks(chain[1:]) if len(chain) > 1 else chain[0]
    if role not in ("lamp", "go2"):
        raise ValueError(f"Unknown cloud agent role: {role}")
    common.update(timeout=35 if role == "lamp" else 30, max_retries=0)
    return model_class(model=os.getenv("DEVICE_TEXT_MODEL", "deepseek-v4-flash-0731"), **common)


def get_vision_model(model_class=None):
    return (model_class or ChatOpenAI)(
        model=os.getenv("GO2_VISION_MODEL", "qwen"),
        base_url=os.getenv("OPENAI_BASE_URL"),
        api_key=os.getenv("OPENAI_API_KEY"),
        temperature=0,
        timeout=30,
    )
