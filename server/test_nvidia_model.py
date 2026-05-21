import os
from pathlib import Path

from dotenv import load_dotenv
from openai import OpenAI


def main():
    load_dotenv(Path(__file__).parent / ".env", override=True)

    base_url = os.getenv("OPENAI_BASE_URL")
    api_key = os.getenv("OPENAI_API_KEY")
    model = os.getenv("MODEL") or os.getenv("MODEL_LIST")

    if not base_url or not api_key or not model:
        raise RuntimeError("Missing OPENAI_BASE_URL, OPENAI_API_KEY, or MODEL/MODEL_LIST")

    model = model.split(",", 1)[0].strip()
    client = OpenAI(base_url=base_url, api_key=api_key, timeout=180.0, max_retries=0)

    stream = client.chat.completions.create(
        model=model,
        max_tokens=4,
        stream=True,
        messages=[
            {"role": "system", "content": "Reply with exactly: ok"},
            {"role": "user", "content": "Connectivity test."},
        ],
    )

    for chunk in stream:
        if chunk.choices and (chunk.choices[0].delta.content or "").strip():
            print("NVIDIA model call ok")
            return

    raise RuntimeError("NVIDIA API stream ended without content")


if __name__ == "__main__":
    main()
