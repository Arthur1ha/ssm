"""One shared text model and one shared vision model for all cloud agents."""

import asyncio
import os
from contextlib import asynccontextmanager
from contextvars import ContextVar

import httpx

from langchain_openai import ChatOpenAI


_async_client: ContextVar[tuple | None] = ContextVar("agent_model_http_client", default=None)


@asynccontextmanager
async def async_model_clients():
    """Own HTTP connections inside the event loop that uses and closes them.

    ChatOpenAI's default client cache is process-wide, so constructing a fresh
    model does not isolate successive asyncio.run() calls. Nested workflows in
    the same loop share this scope; another loop always gets its own client.
    """
    loop = asyncio.get_running_loop()
    existing = _async_client.get()
    if existing and existing[0] is loop and not existing[1].is_closed:
        yield
        return
    async with httpx.AsyncClient() as client:
        token = _async_client.set((loop, client))
        try:
            yield
        finally:
            _async_client.reset(token)


def _async_client_options() -> dict:
    scope = _async_client.get()
    if scope:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return {}
        if scope[0] is loop and not scope[1].is_closed:
            return {"http_async_client": scope[1]}
    return {}


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def get_text_model(role: str, model_class=None):
    """Use the shared text model with each role's request limits."""
    if role not in ("steward", "lamp", "go2"):
        raise ValueError(f"Unknown cloud agent role: {role}")
    model_class = model_class or ChatOpenAI
    model = os.getenv("TEXT_MODEL", "deepseek-v4.1-flash").strip() or "deepseek-v4.1-flash"
    common = {
        "base_url": os.getenv("OPENAI_BASE_URL"),
        "api_key": os.getenv("OPENAI_API_KEY"),
        "temperature": 0,
        **_async_client_options(),
    }
    if role == "steward":
        common.update(timeout=_int_env("LLM_TIMEOUT", 30), max_retries=_int_env("LLM_MAX_RETRIES", 1))
        max_tokens = _int_env("ORCHESTRATOR_MAX_TOKENS", 900)
        if max_tokens > 0:
            common["max_tokens"] = max_tokens
        return model_class(model=model, **common)
    # Compatible device providers accept native tools but may reject a forced
    # function name. LangChain's structured-output wrapper otherwise adds that
    # tool_choice automatically. Keep its parser and schema validation while
    # allowing the provider's default automatic tool selection.
    common.update(timeout=35 if role == "lamp" else 30, max_retries=0,
                  disabled_params={"tool_choice": None})
    return model_class(model=model, **common)


def get_vision_model(model_class=None):
    model = os.getenv("VISION_MODEL", "glm-5.3-flashx").strip() or "glm-5.3-flashx"
    return (model_class or ChatOpenAI)(
        model=model,
        base_url=os.getenv("OPENAI_BASE_URL"),
        api_key=os.getenv("OPENAI_API_KEY"),
        temperature=0,
        timeout=30,
        **_async_client_options(),
    )
