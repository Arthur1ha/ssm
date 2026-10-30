"""Coordinator model policy remains independent of device text models."""

import logging
import os
from langchain_openai import ChatOpenAI

logger = logging.getLogger("orchestrator")

def _int_env(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def _make_llm():
    """构建带 fallback 链的 ChatOpenAI（按 MODEL_LIST 顺序重试）。"""
    model_list_str = os.getenv("MODEL_LIST", os.getenv("MODEL", ""))
    models = [m.strip() for m in model_list_str.split(",") if m.strip()]
    if not models:
        models = ["deepseek-chat"]
    base_url = os.getenv("OPENAI_BASE_URL")
    api_key = os.getenv("OPENAI_API_KEY")
    timeout = _int_env("LLM_TIMEOUT", 30)
    max_retries = _int_env("LLM_MAX_RETRIES", 1)
    max_tokens = _int_env("ORCHESTRATOR_MAX_TOKENS", 900)

    base_kwargs = dict(
        base_url=base_url,
        api_key=api_key,
        temperature=0,
        timeout=timeout,
        max_retries=max_retries,
    )
    if max_tokens > 0:
        base_kwargs["max_tokens"] = max_tokens
    llms = [ChatOpenAI(model=m, **base_kwargs) for m in models]
    logger.info(
        "[Orchestrator] LLM config base_url=%s models=%s key_present=%s timeout=%s max_retries=%s max_tokens=%s",
        base_url or "(empty)",
        " → ".join(models),
        bool(api_key),
        timeout,
        max_retries,
        max_tokens if max_tokens > 0 else "disabled",
    )
    return llms[0].with_fallbacks(llms[1:]) if len(llms) > 1 else llms[0]


