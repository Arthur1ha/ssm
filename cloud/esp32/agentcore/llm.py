"""The lamp's single text model, shared by configuration with Go2."""

import os

from langchain_openai import ChatOpenAI


def get_text_llm(model_class=None) -> ChatOpenAI:
    return (model_class or ChatOpenAI)(
        model=os.getenv("DEVICE_TEXT_MODEL", "deepseek-v4-flash-0731"),
        base_url=os.getenv("OPENAI_BASE_URL"),
        api_key=os.getenv("OPENAI_API_KEY"),
        temperature=0,
        timeout=35,
        max_retries=0,
    )
